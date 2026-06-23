from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.api.routers import account as account_router
from app.core.config import Settings
from app.db.models import AccountSnapshot, AppSetting, DecisionLog, Market, Position
from app.db.session import get_db
from app.services.hyperliquid import HyperliquidOrderResult
from app.services.unified_account_ratio import calculate_unified_account_ratio
from app.worker import uar_guard as worker


def _state(ratio_pct: str, positions: list[dict] | None = None) -> dict:
    return {
        "marginSummary": {"accountValue": "1000", "totalMarginUsed": "100"},
        "crossMarginSummary": {"accountValue": "1000", "totalMarginUsed": "100"},
        "crossMaintenanceMarginUsed": str(Decimal(ratio_pct) * Decimal("10")),
        "withdrawable": "500",
        "assetPositions": positions or [],
    }


def test_unified_account_ratio_uses_cross_equity_plus_free_spot_usdc() -> None:
    ratio = calculate_unified_account_ratio(
        {
            "marginSummary": {"accountValue": "1000"},
            "crossMarginSummary": {"accountValue": "900"},
            "crossMaintenanceMarginUsed": "90",
        },
        free_spot_usdc=Decimal("100"),
    )

    assert ratio.pct == Decimal("9.00")
    assert ratio.status == "ok"
    assert ratio.numerator == Decimal("90")
    assert ratio.denominator == Decimal("1000")


def test_unified_account_ratio_falls_back_to_margin_equity() -> None:
    ratio = calculate_unified_account_ratio(
        {
            "marginSummary": {"accountValue": "1000"},
            "crossMaintenanceMarginUsed": "950",
        }
    )

    assert ratio.pct == Decimal("95.00")
    assert ratio.status == "liquidation_risk"
    assert ratio.source == "cross_maintenance_over_margin_equity_fallback"


def test_unified_account_ratio_unavailable_when_inputs_missing() -> None:
    ratio = calculate_unified_account_ratio({"marginSummary": {"accountValue": "0"}})

    assert ratio.pct is None
    assert ratio.status == "unavailable"


class SnapshotDb:
    def __init__(self, settings: dict[str, dict] | None = None) -> None:
        self.added: list[object] = []
        self.committed = 0
        self.settings = settings or {}

    def get(self, model, key):
        if model is AppSetting:
            if key == "risk":
                return AppSetting(key="risk", value={})
            if key in self.settings:
                return AppSetting(key=key, value=self.settings[key])
        return None

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1

    def query(self, model):
        assert model in (AccountSnapshot, Position)
        return SnapshotQuery()


class SnapshotQuery:
    def order_by(self, *_args):
        return self

    def options(self, *_args):
        return self

    def filter(self, *_args):
        return self

    def first(self):
        return None

    def all(self):
        return []


def test_account_snapshot_exposes_unified_account_ratio_fields(monkeypatch) -> None:
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()

    async def market_meta(_client):
        return {"universe": [{}]}, {"BTC": "100"}, None

    async def account_state(_client, _address, _checked_at):
        return (
            _state("95"),
            {"balances": [{"coin": "USDC", "total": "0", "hold": "0"}]},
            [],
            [],
            None,
        )

    async def predicted(_client):
        return [], None

    monkeypatch.setattr(account_router, "get_settings", lambda: Settings(hyperliquid_account_address="0xabc"))
    monkeypatch.setattr(account_router, "_fetch_market_meta", market_meta)
    monkeypatch.setattr(account_router, "_fetch_account_state", account_state)
    monkeypatch.setattr(account_router, "_fetch_predicted_fundings", predicted)

    db = SnapshotDb()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    response = TestClient(app).get("/api/account/snapshot")

    assert response.status_code == 200
    account = response.json()["account"]
    assert account["unified_account_ratio_pct"] == "95.000000"
    assert account["unified_account_ratio_status"] == "liquidation_risk"
    assert account["unified_account_ratio_threshold_pct"] == "95.000000"
    assert account["unified_account_ratio_numerator"] == "950.000000"
    assert account["unified_account_ratio_denominator"] == "1000.000000"


def test_account_snapshot_uses_short_lived_cache(monkeypatch) -> None:
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()
    calls = {"account_state": 0}

    async def market_meta(_client):
        return {"universe": [{}]}, {"BTC": "100"}, None

    async def account_state(_client, _address, _checked_at):
        calls["account_state"] += 1
        return (
            _state("5"),
            {"balances": [{"coin": "USDC", "total": "0", "hold": "0"}]},
            [],
            [],
            None,
        )

    async def predicted(_client):
        return [], None

    monkeypatch.setattr(account_router, "get_settings", lambda: Settings(hyperliquid_account_address="0xabc"))
    monkeypatch.setattr(account_router, "_fetch_market_meta", market_meta)
    monkeypatch.setattr(account_router, "_fetch_account_state", account_state)
    monkeypatch.setattr(account_router, "_fetch_predicted_fundings", predicted)

    db = SnapshotDb()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    first = client.get("/api/account/snapshot")
    second = client.get("/api/account/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["account_state"] == 1
    assert db.committed == 1
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()


def test_account_snapshot_prefers_fresh_websocket_state(monkeypatch) -> None:
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()
    address = "0xabc"
    settings = Settings(hyperliquid_account_address=address)
    db = SnapshotDb(
        settings={
            "account_data_ws": {"enabled": True, "snapshot_stale_seconds": 90},
            "account_data_ws_state": {
                "connected": True,
                "ready": True,
                "network": settings.hyperliquid_network,
                "account_address": address,
                "updated_at": datetime.now(UTC).isoformat(),
                "user_state": _state("5"),
                "spot_state": {"balances": [{"coin": "USDC", "total": "25", "hold": "5"}]},
                "open_orders": [{"coin": "BTC", "oid": 123}],
                "frontend_open_orders": [{"coin": "BTC", "oid": 123, "isTrigger": False}],
                "predicted_fundings": [],
                "user_funding": [],
            },
        }
    )

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("REST fallback must not run for a fresh websocket snapshot")

    monkeypatch.setattr(
        account_router,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(account_router, "_fetch_market_meta", unexpected)
    monkeypatch.setattr(account_router, "_fetch_account_state", unexpected)
    monkeypatch.setattr(account_router, "_fetch_predicted_fundings", unexpected)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    response = TestClient(app).get("/api/account/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api"]["source"] == "websocket"
    assert payload["api"]["latency_ms"] == 0.0
    assert payload["account"]["effective_account_value"] == "1020.000000"
    assert payload["account"]["open_orders"] == 1
    assert payload["open_orders"][0]["oid"] == 123
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()


def test_stale_account_websocket_state_is_rejected() -> None:
    settings = Settings(hyperliquid_account_address="0xabc")
    db = SnapshotDb(
        settings={
            "account_data_ws": {"enabled": True, "snapshot_stale_seconds": 30},
            "account_data_ws_state": {
                "connected": True,
                "ready": True,
                "network": settings.hyperliquid_network,
                "account_address": settings.hyperliquid_account_address,
                "updated_at": "2020-01-01T00:00:00+00:00",
                "user_state": _state("5"),
                "spot_state": {"balances": []},
                "open_orders": [],
            },
        }
    )

    assert account_router._fresh_account_ws_state(db, settings, datetime.now(UTC)) is None


class GuardQuery:
    def __init__(self, rows: list[Position]) -> None:
        self.rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return list(self.rows)


class GuardDb:
    def __init__(self, risk: dict | None = None, positions: list[Position] | None = None) -> None:
        self.risk = risk or {}
        self.kill_switch: AppSetting | None = None
        self.added: list[object] = []
        self.committed = 0
        self.positions = positions or []

    def get(self, model, key):
        if model is AppSetting and key == "risk":
            return AppSetting(key="risk", value=self.risk)
        if model is AppSetting and key == "kill_switch":
            return self.kill_switch
        if model is AppSetting and key == "live_execution":
            return AppSetting(
                key="live_execution",
                value={"max_slippage_pct": 0.5, "order_expires_after_seconds": 30},
            )
        return None

    def add(self, row: object) -> None:
        if isinstance(row, AppSetting) and row.key == "kill_switch":
            self.kill_switch = row
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1

    def query(self, model):
        assert model is Position
        return GuardQuery(self.positions)


class FakeExecutor:
    def __init__(self) -> None:
        self.settings = Settings(
            hyperliquid_account_address="0xabc",
            hyperliquid_private_key="0x" + "1" * 64,
        )
        self.closed: list[tuple[int | None, Decimal, str]] = []

    def close_position(self, _db, position, exit_price, reason, commit=True):
        self.closed.append((position.id, exit_price, reason))
        position.status = "closed"


class FakeTrading:
    def __init__(self, ratio_pct: str, positions: list[dict] | None = None) -> None:
        self.state = _state(ratio_pct, positions)
        self.closed: list[dict] = []
        self.cancelled: list[tuple[str, int]] = []

    def user_state(self, _address=None, *, max_age_seconds=None):
        return self.state

    def spot_user_state(self, _address=None, *, max_age_seconds=None):
        return {"balances": []}

    def frontend_open_orders(self, _address=None, *, max_age_seconds=None):
        return [{"coin": "ETH", "oid": 123}, {"coin": "BTC", "oid": 456}]

    def cancel_order(self, coin, oid, expires_after_seconds):
        self.cancelled.append((coin, oid))
        return {"status": "ok", "expires_after_seconds": expires_after_seconds}

    def submit_market_close(self, coin, is_buy, size, max_slippage_pct, expires_after_seconds):
        self.closed.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "max_slippage_pct": max_slippage_pct,
                "expires_after_seconds": expires_after_seconds,
            }
        )
        return HyperliquidOrderResult(
            status="ok",
            oid="999",
            avg_price=Decimal("100"),
            filled_size=size,
            raw_response={"status": "ok"},
        )


def _remote_position(coin: str, szi: str, margin: str, pnl: str, value: str) -> dict:
    return {
        "position": {
            "coin": coin,
            "szi": szi,
            "marginUsed": margin,
            "unrealizedPnl": pnl,
            "positionValue": value,
            "entryPx": "100",
        }
    }


def _local_position(coin: str) -> Position:
    market = Market(symbol=coin, exchange="hyperliquid", base=coin, quote="USDC", market_type="perp")
    return Position(id=7, market=market, mode="live", status="open", side="long", entry_price=Decimal("100"), size=Decimal("1"))


def test_uar_guard_does_nothing_below_block_threshold() -> None:
    db = GuardDb()
    trading = FakeTrading("89.9")
    result = worker.audit_unified_account_ratio_guard(db, FakeExecutor(), trading)  # type: ignore[arg-type]

    assert result["status"] == "warning"
    assert db.kill_switch is None
    assert trading.closed == []


def test_uar_guard_arms_kill_switch_at_block_threshold() -> None:
    db = GuardDb()
    trading = FakeTrading("90")
    result = worker.audit_unified_account_ratio_guard(db, FakeExecutor(), trading)  # type: ignore[arg-type]

    assert result["status"] == "critical"
    assert db.kill_switch is not None
    assert db.kill_switch.value["enabled"] is True
    assert db.kill_switch.value["reason"] == "unified_account_ratio_block"
    assert trading.closed == []


def test_uar_guard_closes_one_highest_priority_position_at_derisk_threshold() -> None:
    positions = [
        _remote_position("BTC", "1", "100", "-50", "1000"),
        _remote_position("ETH", "-2", "200", "-10", "2000"),
    ]
    local_position = _local_position("ETH")
    db = GuardDb(positions=[local_position])
    trading = FakeTrading("95", positions)
    executor = FakeExecutor()

    result = worker.audit_unified_account_ratio_guard(db, executor, trading)  # type: ignore[arg-type]

    assert result["status"] == "liquidation_risk"
    assert len(trading.closed) == 1
    assert trading.closed[0]["coin"] == "ETH"
    assert trading.closed[0]["is_buy"] is True
    assert trading.cancelled == [("ETH", 123)]
    assert executor.closed == [(7, Decimal("100"), "unified_account_ratio_derisk")]


def test_uar_guard_logs_critical_ratio_with_no_positions() -> None:
    db = GuardDb()
    trading = FakeTrading("95", [])
    result = worker.audit_unified_account_ratio_guard(db, FakeExecutor(), trading)  # type: ignore[arg-type]

    assert result["status"] == "liquidation_risk"
    assert trading.closed == []
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "unified_account_ratio_derisk_no_positions"
        for row in db.added
    )
