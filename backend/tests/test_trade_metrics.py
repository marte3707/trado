from decimal import Decimal

from app.db.models import Order, Position
from app.services.trade_metrics import build_trade_metrics, exit_pnl, initial_size


def make_position(
    side: str,
    entry: str,
    size: str,
    stop: str | None = None,
    realized_pnl: str | None = None,
    unrealized_pnl: str | None = None,
    metadata: dict | None = None,
    status: str = "closed",
) -> Position:
    return Position(
        id=1,
        market_id=1,
        mode="paper",
        side=side,
        entry_price=Decimal(entry),
        size=Decimal(size),
        stop_loss=Decimal(stop) if stop is not None else None,
        status=status,
        realized_pnl=Decimal(realized_pnl) if realized_pnl is not None else None,
        unrealized_pnl=Decimal(unrealized_pnl) if unrealized_pnl is not None else None,
        metadata_json=metadata or {},
    )


def make_order(order_type: str, price: str, size: str, reduce_only: bool = True) -> Order:
    return Order(
        position_id=1,
        mode="paper",
        order_type=order_type,
        side="short",
        price=Decimal(price),
        size=Decimal(size),
        status="filled",
        reduce_only=reduce_only,
        raw_response={},
    )


def test_exit_pnl_uses_side_direction() -> None:
    long_position = make_position("long", "100", "1")
    short_position = make_position("short", "100", "1")
    exit_order = make_order("take_profit", "95", "2")

    assert exit_pnl(long_position, exit_order) == Decimal("-10")
    assert exit_pnl(short_position, exit_order) == Decimal("10")


def test_metrics_for_short_partial_take_profits_then_stop_are_aggregate_positive() -> None:
    position = make_position(
        "short",
        "15.1990000000",
        "0",
        stop="15.6703431276",
        realized_pnl="217.8750000003",
        unrealized_pnl="0",
        metadata={"initial_size": "424.3193297807608"},
    )
    orders = [
        make_order("tp1", "14.7276568724", "212.1596648904"),
        make_order("tp2", "13.9587783955", "127.2957989342"),
        make_order("stop_loss", "15.6703431276", "84.8638659562"),
    ]

    metrics = build_trade_metrics(position, orders)

    assert metrics.total_pnl == Decimal("217.8750000003")
    assert metrics.realized_return_pct is not None
    assert metrics.realized_r_multiple is not None
    assert metrics.realized_return_pct.quantize(Decimal("0.0001")) == Decimal("3.3783")
    assert metrics.realized_r_multiple.quantize(Decimal("0.0001")) == Decimal("1.0894")


def test_initial_size_falls_back_to_entry_order_for_legacy_positions() -> None:
    position = make_position("long", "100", "0", stop="95", realized_pnl="-25", metadata={})
    orders = [
        make_order("market", "100", "5", reduce_only=False),
        make_order("stop_loss", "95", "5"),
    ]

    assert initial_size(position, orders) == Decimal("5")
    metrics = build_trade_metrics(position, orders)
    assert metrics.notional == Decimal("500")
    assert metrics.realized_r_multiple == Decimal("-1")


def test_breakeven_exit_is_reported_as_zero_pnl() -> None:
    position = make_position("long", "100", "0", stop="95", metadata={"initial_size": "2"})
    orders = [make_order("manual", "100", "2")]

    metrics = build_trade_metrics(position, orders)

    assert metrics.realized_pnl == Decimal("0")
    assert metrics.realized_return_pct == Decimal("0")
    assert metrics.realized_r_multiple == Decimal("0")


def test_open_partial_position_total_pnl_includes_realized_and_unrealized() -> None:
    position = make_position(
        "long",
        "100",
        "5",
        stop="90",
        realized_pnl="25",
        unrealized_pnl="15",
        metadata={"initial_size": "10"},
        status="open",
    )

    metrics = build_trade_metrics(position)

    assert metrics.realized_pnl == Decimal("25")
    assert metrics.unrealized_pnl == Decimal("15")
    assert metrics.total_pnl == Decimal("40")
    assert metrics.return_pct == Decimal("4")
    assert metrics.realized_return_pct == Decimal("2.5")


def test_initial_risk_uses_original_stop_from_metadata_after_trailing() -> None:
    position = make_position(
        "long",
        "100",
        "5",
        stop="105",
        metadata={"initial_size": "10", "initial_stop_loss": "90"},
        status="open",
    )

    metrics = build_trade_metrics(position)

    assert metrics.initial_risk == Decimal("100")
