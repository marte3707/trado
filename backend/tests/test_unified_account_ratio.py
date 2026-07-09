from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.api.routers import account as account_router
from app.core.config import Settings
from app.db.models import AppSetting, DecisionLog, Market, Position
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

    def scalar(self):
        return Decimal("0")


async def _no_account_transfers(_settings, _checked_at):
    return [], None


async def _no_account_performance_components(_settings, _checked_at):
    return account_router._performance_components_payload([], [])


def test_ledger_transfer_payload_filters_deposits_and_withdrawals() -> None:
    deposit = account_router._ledger_transfer_payload(
        {"time": "1700000000000", "hash": "0xabc", "delta": {"type": "deposit", "usdc": "100.25"}}
    )
    withdrawal = account_router._ledger_transfer_payload(
        {"time": 1700000001000, "hash": "0xdef", "delta": {"type": "withdraw", "usdc": "25.5"}}
    )
    spot_to_trading = account_router._ledger_transfer_payload(
        {
            "time": 1700000002000,
            "hash": "0xghi",
            "delta": {
                "type": "send",
                "amount": "65.98",
                "token": "USDC",
                "sourceDex": "spot",
                "destinationDex": "",
            },
        }
    )
    ignored = account_router._ledger_transfer_payload(
        {"time": 1700000003000, "delta": {"type": "referralReward", "usdc": "1"}}
    )

    assert deposit == {
        "time": 1700000000000,
        "kind": "deposit",
        "label": "Deposito",
        "raw_type": "deposit",
        "asset": "USDC",
        "amount": "100.25",
        "signed_amount": "100.25",
        "hash": "0xabc",
    }
    assert withdrawal == {
        "time": 1700000001000,
        "kind": "withdrawal",
        "label": "Prelievo",
        "raw_type": "withdraw",
        "asset": "USDC",
        "amount": "25.5",
        "signed_amount": "-25.5",
        "hash": "0xdef",
    }
    assert spot_to_trading == {
        "time": 1700000002000,
        "kind": "deposit",
        "label": "Deposito",
        "raw_type": "send",
        "asset": "USDC",
        "amount": "65.98",
        "signed_amount": "65.98",
        "hash": "0xghi",
    }
    assert ignored is None


def test_account_performance_reconciles_equity_against_deposits() -> None:
    transfers = [
        {"asset": "USDC", "signed_amount": "65.98"},
        {"asset": "USDC", "signed_amount": "30.29"},
        {"asset": "USDC", "signed_amount": "112"},
        {"asset": "USDC", "signed_amount": "11.86"},
    ]
    performance = account_router._build_account_performance(
        transfers=transfers,
        transfers_error=None,
        effective_account_value=Decimal("145.424233"),
        open_unrealized_pnl=Decimal("-0.327050"),
        components=account_router._performance_components_payload(
            [
                {"closedPnl": "-49.738146", "fee": "22.017151"},
            ],
            [
                {"delta": {"usdc": "-0.863762"}},
            ],
        ),
        local={
            "local_closed_realized_pnl_usdc": Decimal("-39.5857859707"),
            "local_open_unrealized_pnl_usdc": Decimal("-0.7773500000"),
            "local_filled_fees_usdc": Decimal("5.4953050000"),
        },
    )

    assert performance["status"] == "ok"
    assert performance["net_deposits_usdc"] == "220.130000"
    assert performance["account_pnl_usdc"] == "-74.705767"
    assert performance["account_return_pct"] == "-33.937113"
    assert performance["known_components_pnl_usdc"] == "-72.946109"
    assert performance["reconciliation_gap_usdc"] == "-1.759658"
    assert performance["local_closed_realized_pnl_usdc"] == "-39.585786"
    assert performance["fill_count"] == 1
    assert performance["funding_count"] == 1


def test_account_snapshot_exposes_unified_account_ratio_fields(monkeypatch) -> None:
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()
    account_router._ACCOUNT_LEDGER_CACHE.clear()
    account_router._ACCOUNT_PERFORMANCE_CACHE.clear()

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
    monkeypatch.setattr(account_router, "_fetch_account_transfers", _no_account_transfers)
    monkeypatch.setattr(account_router, "_fetch_account_performance_components", _no_account_performance_components)

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
    assert response.json()["ledger"] == {
        "status": "ok",
        "lookback_days": 365,
        "transfers": [],
        "error": None,
    }
    assert response.json()["performance"]["status"] == "unavailable"
    assert response.json()["performance"]["local_closed_realized_pnl_usdc"] == "0.000000"


def test_account_snapshot_uses_short_lived_cache(monkeypatch) -> None:
    account_router._ACCOUNT_SNAPSHOT_CACHE.clear()
    account_router._ACCOUNT_LEDGER_CACHE.clear()
    account_router._ACCOUNT_PERFORMANCE_CACHE.clear()
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
    monkeypatch.setattr(account_router, "_fetch_account_transfers", _no_account_transfers)
    monkeypatch.setattr(account_router, "_fetch_account_performance_components", _no_account_performance_components)

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
    account_router._ACCOUNT_LEDGER_CACHE.clear()
    account_router._ACCOUNT_PERFORMANCE_CACHE.clear()
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
    monkeypatch.setattr(account_router, "_fetch_account_transfers", _no_account_transfers)
    monkeypatch.setattr(account_router, "_fetch_account_performance_components", _no_account_performance_components)

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
