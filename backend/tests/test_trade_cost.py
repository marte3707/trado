from decimal import Decimal

from app.db.models import Order, Position, Signal
from app.services.trade_cost import LiveCostReport, compute_trade_cost, serialize_live_cost_report

TAKER = 0.00045


def _position(
    side: str,
    entry: str,
    size: str,
    realized_pnl: str | None,
    *,
    cum_funding: str | None = None,
    initial_size: str | None = None,
) -> Position:
    meta: dict = {"source_signal_id": 7}
    if initial_size is not None:
        meta["initial_size"] = initial_size
    return Position(
        id=1,
        market_id=1,
        mode="live",
        side=side,
        entry_price=Decimal(entry),
        size=Decimal(size),
        status="closed",
        realized_pnl=Decimal(realized_pnl) if realized_pnl is not None else None,
        cum_funding_since_open=Decimal(cum_funding) if cum_funding is not None else None,
        metadata_json=meta,
    )


def _signal(entry: str) -> Signal:
    return Signal(
        id=7,
        market_id=1,
        timeframe="5m",
        direction="LONG",
        score=70,
        confidence_label="buono",
        entry_price=Decimal(entry),
        reason="x",
    )


def _order(fee: str | None) -> Order:
    return Order(
        position_id=1,
        mode="live",
        order_type="stop_loss",
        side="short",
        size=Decimal("1"),
        status="filled",
        fee=Decimal(fee) if fee is not None else None,
    )


def test_fee_estimated_when_no_tracked_fee():
    # notional = 100 * 2 = 200; fee stimata = 200 * 0.00045 * 2 = 0.18
    pos = _position("long", "100", "2", realized_pnl="10")
    cost = compute_trade_cost(pos, _signal("100"), [], TAKER)
    assert cost.fee_is_estimated is True
    assert cost.fee_usd == Decimal("200") * Decimal("0.00045") * Decimal("2")
    # net = 10 - 0.18 + 0 = 9.82
    assert cost.net_pnl == Decimal("10") - cost.fee_usd


def test_fee_real_when_tracked():
    pos = _position("long", "100", "2", realized_pnl="10")
    cost = compute_trade_cost(pos, _signal("100"), [_order("0.05"), _order("0.04")], TAKER)
    assert cost.fee_is_estimated is False
    assert cost.fee_usd == Decimal("0.09")
    assert cost.net_pnl == Decimal("10") - Decimal("0.09")


def test_slippage_long_unfavorable():
    # entry reale 101 vs segnale 100, long, size 2 → costo positivo 2 USD
    pos = _position("long", "101", "2", realized_pnl="0")
    cost = compute_trade_cost(pos, _signal("100"), [], TAKER)
    assert cost.slippage_entry_usd == Decimal("2")
    # bps su notional segnale 200 → 100 bps
    assert cost.slippage_entry_bps == Decimal("100")


def test_slippage_short_unfavorable():
    # short: vendere a 99 invece di 100 è sfavorevole → costo positivo
    pos = _position("short", "99", "2", realized_pnl="0")
    sig = _signal("100")
    sig.direction = "SHORT"
    cost = compute_trade_cost(pos, sig, [], TAKER)
    assert cost.slippage_entry_usd == Decimal("2")


def test_funding_included_in_net():
    pos = _position("long", "100", "2", realized_pnl="10", cum_funding="-0.5")
    cost = compute_trade_cost(pos, _signal("100"), [], TAKER)
    assert cost.funding_usd == Decimal("-0.5")
    assert cost.net_pnl == Decimal("10") - cost.fee_usd + Decimal("-0.5")


def test_initial_size_metadata_preferred():
    # size corrente 1 (dopo chiusure parziali) ma initial_size 2 → notional usa 2
    pos = _position("long", "100", "1", realized_pnl="0", initial_size="2")
    cost = compute_trade_cost(pos, _signal("100"), [], TAKER)
    assert cost.notional == Decimal("200")


def test_serialize_empty_report():
    report = LiveCostReport(
        trades=0,
        gross_pnl=Decimal("0"),
        fee_usd=Decimal("0"),
        funding_usd=Decimal("0"),
        slippage_usd=Decimal("0"),
        net_pnl=Decimal("0"),
        fee_tracked_trades=0,
        fee_estimated_trades=0,
        avg_slippage_bps=None,
        taker_fee_rate=TAKER,
        items=[],
    )
    payload = serialize_live_cost_report(report)
    assert payload["trades"] == 0
    assert payload["avg_slippage_bps"] is None
    assert payload["items"] == []
