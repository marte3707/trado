from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.db.models import Order, Position

EXIT_ORDER_TYPES = {"tp1", "tp2", "tp3", "take_profit", "stop_loss", "manual"}


@dataclass(frozen=True)
class TradeMetrics:
    initial_size: Decimal | None
    notional: Decimal | None
    initial_risk: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    total_pnl: Decimal | None
    return_pct: Decimal | None
    realized_return_pct: Decimal | None
    r_multiple: Decimal | None
    realized_r_multiple: Decimal | None


def build_trade_metrics(
    position: Position,
    orders: Iterable[Order] = (),
    unrealized_pnl: Decimal | None = None,
) -> TradeMetrics:
    order_list = list(orders)
    initial = initial_size(position, order_list)
    notional = position.entry_price * initial if initial is not None else None
    risk = initial_risk(position, initial)
    realized = realized_pnl(position, order_list)
    unrealized = unrealized_pnl if unrealized_pnl is not None else position.unrealized_pnl
    total = add_optional(realized, unrealized)
    return TradeMetrics(
        initial_size=initial,
        notional=notional,
        initial_risk=risk,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=total,
        return_pct=ratio_pct(total, notional),
        realized_return_pct=ratio_pct(realized, notional),
        r_multiple=ratio(total, risk),
        realized_r_multiple=ratio(realized, risk),
    )


def initial_size(position: Position, orders: Iterable[Order] = ()) -> Decimal | None:
    metadata = position.metadata_json or {}
    metadata_size = _decimal(metadata.get("initial_size"))
    if metadata_size is not None and metadata_size > 0:
        return metadata_size

    order_list = list(orders)
    entry_size = sum(
        (order.size for order in order_list if order.order_type not in EXIT_ORDER_TYPES and not order.reduce_only),
        Decimal("0"),
    )
    if entry_size > 0:
        return entry_size

    reconstructed = position.size + sum(
        (order.size for order in order_list if order.order_type in EXIT_ORDER_TYPES or order.reduce_only),
        Decimal("0"),
    )
    if reconstructed > 0:
        return reconstructed
    if position.size > 0:
        return position.size
    return None


def initial_risk(position: Position, size: Decimal | None = None) -> Decimal | None:
    metadata = position.metadata_json or {}
    stop_loss = _decimal(metadata.get("initial_stop_loss")) or position.stop_loss
    if stop_loss is None:
        return None
    initial = size if size is not None else initial_size(position)
    if initial is None:
        return None
    risk = abs(position.entry_price - stop_loss) * initial
    return risk if risk > 0 else None


def realized_pnl(position: Position, orders: Iterable[Order] = ()) -> Decimal | None:
    if position.realized_pnl is not None:
        return position.realized_pnl
    exit_orders = [order for order in orders if is_exit_order(order) and order.status == "filled"]
    if not exit_orders:
        return None
    return sum((exit_pnl(position, order) for order in exit_orders), Decimal("0"))


def exit_pnl(position: Position, order: Order) -> Decimal:
    if order.price is None:
        return Decimal("0")
    if position.side.lower() == "short":
        return (position.entry_price - order.price) * order.size
    return (order.price - position.entry_price) * order.size


def is_exit_order(order: Order) -> bool:
    return order.order_type in EXIT_ORDER_TYPES or bool(order.reduce_only)


def add_optional(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None and right is None:
        return None
    return (left or Decimal("0")) + (right or Decimal("0"))


def ratio(value: Decimal | None, base: Decimal | None) -> Decimal | None:
    if value is None or base is None or base == 0:
        return None
    return value / base


def ratio_pct(value: Decimal | None, base: Decimal | None) -> Decimal | None:
    result = ratio(value, base)
    return result * Decimal("100") if result is not None else None


def _decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
