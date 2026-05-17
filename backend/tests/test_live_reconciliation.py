from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.db.models import DecisionLog, Market, Order, Position
from app.services.live_reconciliation import reconcile_live_positions


@dataclass
class _Query:
    rows: list[Any]

    def options(self, *args, **kwargs) -> "_Query":
        return self

    def filter(self, *_args, **_kwargs) -> "_Query":
        return self

    def all(self) -> list[Any]:
        return list(self.rows)


@dataclass
class FakeDb:
    positions: list[Position]
    orders: list[Order] = field(default_factory=list)
    added: list[Any] = field(default_factory=list)
    committed: int = 0

    def query(self, model: type[Any]) -> _Query:
        if model is Order:
            return _Query(rows=self.orders)
        return _Query(rows=self.positions)

    def get(self, _model: type[Any], _key: Any) -> None:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1


@dataclass
class _FakeOrderResult:
    oid: str | None
    status: str
    raw_response: dict[str, Any] = field(default_factory=dict)
    avg_price: Any = None
    filled_size: Any = None


class FakeTrading:
    def __init__(
        self,
        remote_positions: list[dict[str, Any]],
        fills: list[dict[str, Any]] | None = None,
        open_order_oids: list[int] | None = None,
        resubmit_oid: str = "88888",
    ) -> None:
        self._remote = remote_positions
        self._fills = fills or []
        # oids of orders currently open on HL (for protection-order detection tests)
        self._open_order_oids: list[int] | None = open_order_oids
        self._resubmit_oid = resubmit_oid
        self.resubmitted: list[dict[str, Any]] = []

    def user_state(self, _address: str) -> dict[str, Any]:
        return {"assetPositions": [{"position": p} for p in self._remote]}

    def user_fills(self, _address: str) -> list[dict[str, Any]]:
        return list(self._fills)

    def open_orders(self, _address: str) -> list[dict[str, Any]]:
        if self._open_order_oids is None:
            raise AttributeError("open_orders not configured")
        return [{"oid": oid} for oid in self._open_order_oids]

    def submit_trigger_order(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: Any,
        trigger_price: Any,
        tpsl: str,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> _FakeOrderResult:
        self.resubmitted.append(
            {"coin": coin, "is_buy": is_buy, "size": size, "tpsl": tpsl}
        )
        return _FakeOrderResult(oid=self._resubmit_oid, status="ok")


def _make_market() -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC"},
    )


def _make_position(size: str = "1") -> Position:
    p = Position(
        id=10,
        market_id=1,
        mode="live",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal(size),
        stop_loss=None,
        take_profit=None,
        status="open",
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        metadata_json={},
    )
    p.market = _make_market()
    return p


@pytest.fixture(autouse=True)
def _ensure_hl_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "hyperliquid_account_address", "0xabc", raising=False)


def test_reconciliation_closes_position_missing_remote() -> None:
    position = _make_position()
    db = FakeDb(positions=[position])
    trading = FakeTrading(remote_positions=[])

    updated = reconcile_live_positions(db, trading_client=trading)

    assert updated == 1
    assert position.status == "closed"
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_position_reconciled_closed" in events


def test_reconciliation_does_not_derive_mark_px_from_unrealized() -> None:
    position = _make_position(size="1")
    db = FakeDb(positions=[position])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "1", "entryPx": "100", "unrealizedPnl": "5"}]
    )

    reconcile_live_positions(db, trading_client=trading)

    # markPx not provided → must NOT be derived/stored
    assert "last_mark_price" not in (position.metadata_json or {})
    # but unrealized + pnl_updated_at must be populated
    assert position.unrealized_pnl == Decimal("5")
    assert position.pnl_updated_at is not None


def test_reconciliation_stores_mark_px_when_remote_provides_it() -> None:
    position = _make_position(size="1")
    db = FakeDb(positions=[position])
    trading = FakeTrading(
        remote_positions=[
            {"coin": "BTC", "szi": "1", "entryPx": "100", "unrealizedPnl": "5", "markPx": "105"}
        ]
    )

    reconcile_live_positions(db, trading_client=trading)

    assert position.metadata_json["last_mark_price"] == "105"


def test_reconciliation_flags_size_mismatch_on_first_tick() -> None:
    position = _make_position(size="1.0")
    db = FakeDb(positions=[position])
    # remote size 1.5 → divergence (1.5-1)/1.5 ≈ 33% well above 5% tolerance
    trading = FakeTrading(remote_positions=[{"coin": "BTC", "szi": "1.5"}])

    reconcile_live_positions(db, trading_client=trading)

    # local size NOT overwritten on the first divergence tick
    assert position.size == Decimal("1.0")
    assert position.metadata_json.get("size_mismatch_at") is not None
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_reconciliation_size_mismatch" in events


def test_reconciliation_applies_size_on_second_tick_after_mismatch() -> None:
    position = _make_position(size="1.0")
    position.metadata_json = {"size_mismatch_at": "2026-01-01T00:00:00+00:00"}
    db = FakeDb(positions=[position])
    trading = FakeTrading(remote_positions=[{"coin": "BTC", "szi": "1.5"}])

    reconcile_live_positions(db, trading_client=trading)

    # Second tick: apply remote size + clean the mismatch marker.
    assert position.size == Decimal("1.5")
    assert "size_mismatch_at" not in position.metadata_json


def test_reconciliation_small_divergence_within_tolerance_applies_immediately() -> None:
    position = _make_position(size="1.000")
    db = FakeDb(positions=[position])
    # remote size 1.02 → divergence (1.02-1)/1.02 ≈ 1.96% < 5%
    trading = FakeTrading(remote_positions=[{"coin": "BTC", "szi": "1.02"}])

    reconcile_live_positions(db, trading_client=trading)

    assert position.size == Decimal("1.02")
    assert "size_mismatch_at" not in (position.metadata_json or {})


def _make_tp_order(
    cloid: str = "abc123",
    level: str = "tp1",
    size: str = "0.5",
    price: str = "110",
    oid_str: str = "9999",
) -> Order:
    return Order(
        id=42,
        position_id=10,
        signal_id=None,
        hyperliquid_order_id=oid_str,
        cloid=cloid,
        mode="live",
        order_type=level,
        side="sell",
        price=Decimal(price),
        size=Decimal(size),
        status="resting",
        reduce_only=True,
        raw_response={},
    )


def test_reconciliation_marks_tp1_filled_from_hl_fills() -> None:
    position = _make_position(size="1.0")
    position.metadata_json = {
        "take_profit_plan": [
            {"level": "tp1", "price": "110", "allocation": "0.5", "filled": False},
            {"level": "tp2", "price": "120", "allocation": "0.3", "filled": False},
            {"level": "tp3", "price": "130", "allocation": "0.2", "filled": False},
        ]
    }
    tp1_order = _make_tp_order()
    db = FakeDb(positions=[position], orders=[tp1_order])
    # Remote position shows half size left after TP1 fired
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        fills=[
            {
                "coin": "BTC",
                "oid": 9999,
                "cloid": "0xabc123",
                "px": "110",
                "sz": "0.5",
                "side": "A",
                "dir": "Close Long",
                "closedPnl": "5",
                "time": 1700000000000,
            }
        ],
    )

    reconcile_live_positions(db, trading_client=trading)

    # Order finalized
    assert tp1_order.status == "filled"
    assert tp1_order.price == Decimal("110")
    assert tp1_order.size == Decimal("0.5")
    # Realized PnL accumulated from HL closedPnl
    assert position.realized_pnl == Decimal("5")
    # TP plan entry marked filled
    plan = position.metadata_json["take_profit_plan"]
    assert plan[0]["filled"] is True
    assert plan[0]["filled_price"] == "110"
    assert plan[1]["filled"] is False
    # Local size aligned to remote
    assert position.size == Decimal("0.5")
    # DecisionLog event recorded
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_reconciliation_tp1_filled" in events


def test_reconciliation_tp_fill_suppresses_size_mismatch_warning() -> None:
    """A 50% drop in size with a matching TP fill must NOT trigger the
    size-mismatch protection — the divergence is fully explained."""
    position = _make_position(size="1.0")
    position.metadata_json = {
        "take_profit_plan": [
            {"level": "tp1", "price": "110", "allocation": "0.5", "filled": False},
        ]
    }
    tp1_order = _make_tp_order()
    db = FakeDb(positions=[position], orders=[tp1_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        fills=[
            {
                "coin": "BTC",
                "oid": 9999,
                "cloid": "0xabc123",
                "px": "110",
                "sz": "0.5",
                "closedPnl": "5",
            }
        ],
    )

    reconcile_live_positions(db, trading_client=trading)

    assert "size_mismatch_at" not in (position.metadata_json or {})
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_reconciliation_size_mismatch" not in events


def test_reconciliation_aligns_local_stop_size_after_partial_fill() -> None:
    position = _make_position(size="1.0")
    tp1_order = _make_tp_order()
    stop_order = _make_sl_order()
    db = FakeDb(positions=[position], orders=[tp1_order, stop_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        fills=[
            {
                "coin": "BTC",
                "oid": 9999,
                "cloid": "0xabc123",
                "px": "110",
                "sz": "0.5",
                "closedPnl": "5",
            }
        ],
    )

    reconcile_live_positions(db, trading_client=trading)

    assert stop_order.size == Decimal("0.5")
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_stop_order_size_reconciled" in events


def test_reconciliation_matches_fill_by_oid_when_cloid_missing() -> None:
    position = _make_position(size="1.0")
    tp1_order = _make_tp_order(cloid=None)  # no cloid stored
    tp1_order.cloid = None
    db = FakeDb(positions=[position], orders=[tp1_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        fills=[
            {
                "coin": "BTC",
                "oid": 9999,
                "px": "110",
                "sz": "0.5",
                "closedPnl": "5",
            }
        ],
    )

    reconcile_live_positions(db, trading_client=trading)

    assert tp1_order.status == "filled"
    assert position.realized_pnl == Decimal("5")


def test_reconciliation_skips_already_filled_orders() -> None:
    position = _make_position(size="0.5")
    position.realized_pnl = Decimal("5")  # already accounted for
    tp1_order = _make_tp_order()
    tp1_order.status = "filled"  # already finalized
    db = FakeDb(positions=[position], orders=[tp1_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        fills=[
            {
                "coin": "BTC",
                "oid": 9999,
                "cloid": "0xabc123",
                "px": "110",
                "sz": "0.5",
                "closedPnl": "5",
            }
        ],
    )

    reconcile_live_positions(db, trading_client=trading)

    # PnL must NOT be double-counted
    assert position.realized_pnl == Decimal("5")
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_reconciliation_tp1_filled" not in events


# ---------------------------------------------------------------------------
# Protection-order resubmission tests
# ---------------------------------------------------------------------------

def _make_sl_order(oid: int = 8888) -> Order:
    return Order(
        id=55,
        position_id=10,
        signal_id=None,
        hyperliquid_order_id=str(oid),
        cloid="slcloid",
        mode="live",
        order_type="stop_loss",
        side="buy",
        price=Decimal("90"),
        size=Decimal("1.0"),
        status="ok",
        reduce_only=True,
        raw_response={},
    )


def test_resubmit_missing_tp1_when_not_in_hl_open_orders() -> None:
    """TP1 order oid absent from HL open orders → must be re-submitted."""
    position = _make_position(size="1.0")
    tp1_order = _make_tp_order(oid_str="9999")
    db = FakeDb(
        positions=[position],
        orders=[tp1_order],
    )
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "1.0", "entryPx": "100"}],
        # open_order_oids is empty → tp1 oid 9999 is absent
        open_order_oids=[],
        resubmit_oid="77777",
    )

    reconcile_live_positions(db, trading_client=trading)

    assert len(trading.resubmitted) == 1
    assert trading.resubmitted[0]["tpsl"] == "tp"
    assert tp1_order.hyperliquid_order_id == "77777"
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_protection_order_resubmitted" in events


def test_no_resubmit_when_tp1_present_in_hl_open_orders() -> None:
    """TP1 oid found in HL open orders → no re-submission."""
    position = _make_position(size="1.0")
    tp1_order = _make_tp_order(oid_str="9999")
    db = FakeDb(positions=[position], orders=[tp1_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "1.0", "entryPx": "100"}],
        open_order_oids=[9999],  # tp1 oid present
    )

    reconcile_live_positions(db, trading_client=trading)

    assert trading.resubmitted == []
    events = [r.event_type for r in db.added if isinstance(r, DecisionLog)]
    assert "live_protection_order_resubmitted" not in events


def test_no_resubmit_when_open_orders_api_fails() -> None:
    """If HL open_orders() raises, resubmission must be skipped entirely."""
    position = _make_position(size="1.0")
    tp1_order = _make_tp_order(oid_str="9999")
    db = FakeDb(positions=[position], orders=[tp1_order])
    # open_order_oids=None → open_orders() raises AttributeError
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "1.0", "entryPx": "100"}],
        open_order_oids=None,
    )

    reconcile_live_positions(db, trading_client=trading)

    assert trading.resubmitted == []


def test_no_resubmit_for_terminal_status_order() -> None:
    """A protection order already marked 'filled' or 'canceled' must not be re-submitted."""
    position = _make_position(size="0.5")
    tp1_order = _make_tp_order(oid_str="9999")
    tp1_order.status = "filled"
    db = FakeDb(positions=[position], orders=[tp1_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "0.5", "entryPx": "100"}],
        open_order_oids=[],  # oid absent, but status is terminal
    )

    reconcile_live_positions(db, trading_client=trading)

    assert trading.resubmitted == []


def test_resubmit_sl_order_with_correct_tpsl_flag() -> None:
    """A missing stop_loss order must be re-submitted with tpsl='sl'."""
    position = _make_position(size="1.0")
    sl_order = _make_sl_order(oid=8888)
    db = FakeDb(positions=[position], orders=[sl_order])
    trading = FakeTrading(
        remote_positions=[{"coin": "BTC", "szi": "1.0", "entryPx": "100"}],
        open_order_oids=[],  # 8888 absent
        resubmit_oid="66666",
    )

    reconcile_live_positions(db, trading_client=trading)

    assert len(trading.resubmitted) == 1
    assert trading.resubmitted[0]["tpsl"] == "sl"
    assert sl_order.hyperliquid_order_id == "66666"
