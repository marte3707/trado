from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.enums import OrderMode, SignalStatus
from app.core.runtime_settings import effective_live_execution_settings
from app.db.models import DecisionLog, Market, Order, Position, Signal
from app.engine.position_management import PositionManagementEngine, StopCandidate
from app.services.hyperliquid import HyperliquidTradingClient

logger = structlog.get_logger()

# Relative tolerance allowed between local and remote position size before we
# log a reconciliation_mismatch and refuse to overwrite the local size on the
# first tick. 5% absorbs rounding, contract decimals and tiny fills.
SIZE_DIVERGENCE_TOLERANCE = Decimal("0.05")

# Order types whose fills we reconcile against Hyperliquid (reduce-only exits).
_RECONCILED_EXIT_TYPES = ("tp1", "tp2", "tp3", "stop_loss")
# Order statuses we still consider "pending" — anything else (filled/canceled)
# is terminal and not eligible for fill reconciliation.
_TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled"})

# Maps our internal order_type to the tpsl string expected by Hyperliquid.
_TPSL_BY_ORDER_TYPE: dict[str, str] = {
    "tp1": "tp",
    "tp2": "tp",
    "tp3": "tp",
    "stop_loss": "sl",
}

# After this many consecutive failed re-submissions we stop retrying to avoid
# infinite resubmit loops when HL persistently rejects the order.
_MAX_RESUBMIT_ATTEMPTS = 3


@dataclass(frozen=True)
class _ExitFillMatch:
    realized_delta: Decimal
    last_exit_price: Decimal | None
    last_exit_reason: str | None


def _decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _coin_for_market(market: Market) -> str:
    raw = (market.metadata_json or {}).get("hl_coin")
    return raw if isinstance(raw, str) and raw else market.symbol


def _position_map(user_state: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in user_state.get("assetPositions", []):
        position = item.get("position", {})
        coin = position.get("coin")
        if isinstance(coin, str):
            result[coin] = position
    return result


def _size_divergence(local: Decimal, remote_abs: Decimal) -> Decimal:
    base = max(abs(local), abs(remote_abs))
    if base == 0:
        return Decimal("0")
    return abs(local - remote_abs) / base


def _initial_risk_per_unit(position: Position) -> Decimal | None:
    metadata = position.metadata_json or {}
    risk = _decimal(metadata.get("initial_risk_per_unit"))
    if risk is not None and risk > 0:
        return risk
    initial_stop = _decimal(metadata.get("initial_stop_loss")) or position.stop_loss
    if initial_stop is None:
        return None
    risk = abs(position.entry_price - initial_stop)
    return risk if risk > 0 else None


def _position_r_multiple(position: Position, price: Decimal) -> Decimal:
    risk = _initial_risk_per_unit(position)
    if risk is None or risk <= 0:
        return Decimal("0")
    if position.side.lower() == "short":
        return (position.entry_price - price) / risk
    return (price - position.entry_price) / risk


def _needs_tp1_breakeven_stop(position: Position) -> bool:
    metadata = position.metadata_json or {}
    if not metadata.get("tp1_hit") or metadata.get("breakeven_triggered"):
        return False
    entry = position.entry_price
    if entry is None or entry <= 0:
        return False
    stop = position.stop_loss
    side = position.side.lower()
    if side == "long":
        return stop is None or stop < entry
    if side == "short":
        return stop is None or stop > entry
    return False


def _reference_price_for_tp1_breakeven(
    position: Position,
    remote_position: dict,
    fill_match: _ExitFillMatch,
) -> Decimal | None:
    mark_px = _decimal(remote_position.get("markPx"))
    if mark_px is not None and mark_px > 0:
        return mark_px
    if fill_match.last_exit_reason == "tp1":
        return fill_match.last_exit_price
    last_mark = _decimal((position.metadata_json or {}).get("last_mark_price"))
    return last_mark if last_mark is not None and last_mark > 0 else None


def _protect_stop_at_breakeven_after_tp1(
    db: Session,
    position: Position,
    market: Market,
    remote_position: dict,
    fill_match: _ExitFillMatch,
    trading: HyperliquidTradingClient,
) -> bool:
    if not _needs_tp1_breakeven_stop(position):
        return False

    reference_price = _reference_price_for_tp1_breakeven(position, remote_position, fill_match)
    side = position.side.lower()
    entry = position.entry_price
    if reference_price is not None:
        if side == "long" and reference_price <= entry:
            return False
        if side == "short" and reference_price >= entry:
            return False

    candidate = StopCandidate(
        price=entry,
        reason="breakeven_after_tp1",
        indicators={
            "current_price": str(reference_price) if reference_price is not None else None,
            "source": "live_reconciliation",
            "trigger": fill_match.last_exit_reason or "tp1_hit_metadata",
        },
        r_multiple=_position_r_multiple(position, reference_price or entry),
    )
    return PositionManagementEngine(get_settings()).replace_live_stop_order(
        db,
        market,
        position,
        candidate,
        trading_client=trading,
    )


def _needs_tp2_lock_profit_stop(position: Position) -> bool:
    metadata = position.metadata_json or {}
    if not metadata.get("tp2_hit"):
        return False
    risk = _initial_risk_per_unit(position)
    if risk is None or risk <= 0:
        return False
    entry = position.entry_price
    if entry is None or entry <= 0:
        return False
    config = metadata.get("position_management_config") or {}
    trailing = config.get("trailingStop") or {} if isinstance(config, dict) else {}
    lock_r = _decimal(trailing.get("lockProfitStopR")) or Decimal("1")
    side = position.side.lower()
    lock_price = entry + risk * lock_r if side == "long" else entry - risk * lock_r
    stop = position.stop_loss
    if side == "long":
        return stop is None or stop < lock_price
    if side == "short":
        return stop is None or stop > lock_price
    return False


def _reference_price_for_tp2_lock_profit(
    position: Position,
    remote_position: dict,
    fill_match: _ExitFillMatch,
) -> Decimal | None:
    mark_px = _decimal(remote_position.get("markPx"))
    if mark_px is not None and mark_px > 0:
        return mark_px
    if fill_match.last_exit_reason == "tp2":
        return fill_match.last_exit_price
    last_mark = _decimal((position.metadata_json or {}).get("last_mark_price"))
    return last_mark if last_mark is not None and last_mark > 0 else None


def _protect_stop_lock_profit_after_tp2(
    db: Session,
    position: Position,
    market: Market,
    remote_position: dict,
    fill_match: _ExitFillMatch,
    trading: HyperliquidTradingClient,
) -> bool:
    if not _needs_tp2_lock_profit_stop(position):
        return False
    risk = _initial_risk_per_unit(position)
    if risk is None:
        return False
    metadata = position.metadata_json or {}
    config = metadata.get("position_management_config") or {}
    trailing = config.get("trailingStop") or {} if isinstance(config, dict) else {}
    lock_r = _decimal(trailing.get("lockProfitStopR")) or Decimal("1")
    entry = position.entry_price
    side = position.side.lower()
    lock_price = entry + risk * lock_r if side == "long" else entry - risk * lock_r
    reference_price = _reference_price_for_tp2_lock_profit(position, remote_position, fill_match)
    if reference_price is not None:
        if side == "long" and reference_price <= lock_price:
            return False
        if side == "short" and reference_price >= lock_price:
            return False
    candidate = StopCandidate(
        price=lock_price,
        reason="lock_profit_after_tp2",
        indicators={
            "current_price": str(reference_price) if reference_price is not None else None,
            "lock_profit_stop_r": str(lock_r),
            "source": "live_reconciliation",
            "trigger": fill_match.last_exit_reason or "tp2_hit_metadata",
        },
        r_multiple=_position_r_multiple(position, reference_price or lock_price),
    )
    return PositionManagementEngine(get_settings()).replace_live_stop_order(
        db,
        market,
        position,
        candidate,
        trading_client=trading,
    )


def _index_fills(fills: list[dict]) -> tuple[dict[str, list[dict]], dict[int, list[dict]]]:
    by_cloid: dict[str, list[dict]] = {}
    by_oid: dict[int, list[dict]] = {}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        cloid = fill.get("cloid")
        if isinstance(cloid, str) and cloid:
            # Hyperliquid returns cloid as a 0x-prefixed hex string; we store
            # them lowercase without the prefix, so normalize both sides.
            key = cloid.lower().removeprefix("0x")
            by_cloid.setdefault(key, []).append(fill)
        oid = fill.get("oid")
        if isinstance(oid, int):
            by_oid.setdefault(oid, []).append(fill)
    return by_cloid, by_oid


def _reconcile_exit_fills(
    db: Session,
    position: Position,
    coin: str,
    fills_by_cloid: dict[str, list[dict]],
    fills_by_oid: dict[int, list[dict]],
) -> _ExitFillMatch:
    """Match pending TP/SL orders against HL fills and finalize them.

    Returns the realized-PnL delta to add to the position. Mutates the
    position's metadata (take_profit_plan filled flags) in place but does not
    persist the position itself — caller is responsible for db.add(position).
    """
    pending_orders = (
        db.query(Order)
        .filter(
            Order.position_id == position.id,
            Order.mode == OrderMode.LIVE.value,
            Order.order_type.in_(_RECONCILED_EXIT_TYPES),
            Order.status.notin_(_TERMINAL_ORDER_STATUSES),
        )
        .all()
    )
    if not pending_orders:
        return _ExitFillMatch(
            realized_delta=Decimal("0"),
            last_exit_price=None,
            last_exit_reason=None,
        )

    metadata = position.metadata_json or {}
    plan = metadata.get("take_profit_plan")
    plan_mut = plan if isinstance(plan, list) else None
    realized_delta = Decimal("0")
    last_exit_price: Decimal | None = None
    last_exit_reason: str | None = None

    for order in pending_orders:
        # Defensive: SQL filter should already exclude these, but never
        # re-process a terminal order (would double-count PnL).
        if order.status in _TERMINAL_ORDER_STATUSES:
            continue
        matched: list[dict] = []
        if order.cloid:
            key = order.cloid.lower().removeprefix("0x")
            matched = list(fills_by_cloid.get(key, ()))
        if not matched and order.hyperliquid_order_id:
            try:
                oid_int = int(order.hyperliquid_order_id)
            except (TypeError, ValueError):
                oid_int = None
            if oid_int is not None:
                matched = list(fills_by_oid.get(oid_int, ()))
        if not matched:
            continue

        total_sz = Decimal("0")
        total_pnl = Decimal("0")
        total_notional = Decimal("0")
        for fill in matched:
            sz = _decimal(fill.get("sz")) or Decimal("0")
            px = _decimal(fill.get("px")) or Decimal("0")
            pnl = _decimal(fill.get("closedPnl")) or Decimal("0")
            total_sz += sz
            total_pnl += pnl
            total_notional += sz * px
        avg_px = total_notional / total_sz if total_sz > 0 else None

        order.status = "filled"
        if avg_px is not None:
            order.price = avg_px
        if total_sz > 0:
            order.size = total_sz
        if not order.hyperliquid_order_id:
            oid_val = matched[0].get("oid")
            if oid_val is not None:
                order.hyperliquid_order_id = str(oid_val)
        raw = dict(order.raw_response or {})
        raw["fills"] = matched
        order.raw_response = raw
        db.add(order)

        if plan_mut is not None and order.order_type in {"tp1", "tp2", "tp3"}:
            for entry in plan_mut:
                if isinstance(entry, dict) and entry.get("level") == order.order_type:
                    entry["filled"] = True
                    if avg_px is not None:
                        entry["filled_price"] = str(avg_px)
                    entry["filled_size"] = str(total_sz)
            metadata["partial_exit_count"] = int(metadata.get("partial_exit_count") or 0) + 1
            if order.order_type == "tp1":
                metadata["tp1_hit"] = True
            elif order.order_type == "tp2":
                metadata["tp2_hit"] = True
                metadata["runner_active"] = True

        realized_delta += total_pnl
        if avg_px is not None:
            last_exit_price = avg_px
            last_exit_reason = order.order_type
        db.add(
            DecisionLog(
                event_type=f"live_reconciliation_{order.order_type}_filled",
                market_id=position.market_id,
                position_id=position.id,
                message=f"Reconciled live {order.order_type} fill: {total_sz} @ {avg_px}",
                data={
                    "coin": coin,
                    "order_id": order.id,
                    "cloid": order.cloid,
                    "oid": order.hyperliquid_order_id,
                    "size": str(total_sz),
                    "avg_price": str(avg_px) if avg_px is not None else None,
                    "closed_pnl": str(total_pnl),
                },
            )
        )

    return _ExitFillMatch(
        realized_delta=realized_delta,
        last_exit_price=last_exit_price,
        last_exit_reason=last_exit_reason,
    )


def _sz_decimals(market: Market | None) -> int:
    if market is None:
        return 8
    try:
        return int((market.metadata_json or {}).get("raw", {}).get("szDecimals", 8))
    except (TypeError, ValueError):
        return 8


def _round_size(size: Decimal, decimals: int) -> Decimal:
    return size.quantize(Decimal(10) ** -decimals)


def _tp_allocation_map(position: Position) -> dict[str, Decimal]:
    """Return {level: allocation} from the position's take_profit_plan metadata."""
    plan = (position.metadata_json or {}).get("take_profit_plan") or []
    result: dict[str, Decimal] = {}
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        level = entry.get("level")
        alloc = _decimal(entry.get("allocation"))
        if level and alloc is not None and alloc > 0:
            result[str(level)] = alloc
    return result


def _index_open_orders(open_orders: list[dict]) -> tuple[set[int], set[str]]:
    """Return (oids, normalised_cloids) for quick membership tests."""
    oids: set[int] = set()
    cloids: set[str] = set()
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        oid = order.get("oid")
        if isinstance(oid, int):
            oids.add(oid)
        cloid = order.get("cloid")
        if isinstance(cloid, str) and cloid:
            cloids.add(cloid.lower().removeprefix("0x"))
    return oids, cloids


def _index_frontend_open_orders(frontend_open_orders: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for order in frontend_open_orders:
        if not isinstance(order, dict):
            continue
        coin = order.get("coin")
        if isinstance(coin, str) and coin:
            grouped.setdefault(coin, []).append(order)
    return grouped


def _frontend_order_tpsl(order: dict, position: Position) -> str | None:
    if not bool(order.get("reduceOnly")) or not bool(order.get("isTrigger")):
        return None
    order_type = str(order.get("orderType") or "").strip().lower()
    if "take profit" in order_type:
        return "tp"
    if "stop" in order_type:
        return "sl"

    trigger_px = _decimal(order.get("triggerPx"))
    if trigger_px is None:
        return None
    side = position.side.lower()
    if side == "long":
        return "tp" if trigger_px >= position.entry_price else "sl"
    if side == "short":
        return "tp" if trigger_px <= position.entry_price else "sl"
    return None


def _match_frontend_protection_order(
    frontend_orders: list[dict],
    order: Order,
    position: Position,
) -> dict | None:
    desired_tpsl = _TPSL_BY_ORDER_TYPE.get(order.order_type)
    if desired_tpsl is None:
        return None

    matches: list[dict] = [
        item
        for item in frontend_orders
        if _frontend_order_tpsl(item, position) == desired_tpsl
    ]
    if not matches:
        return None

    if desired_tpsl == "tp" and order.price is not None:
        for item in matches:
            trigger_px = _decimal(item.get("triggerPx"))
            if trigger_px == order.price:
                return item
        return None

    if desired_tpsl == "sl" and order.price is not None:
        for item in matches:
            trigger_px = _decimal(item.get("triggerPx"))
            if trigger_px == order.price:
                return item

    return matches[0]


def _sync_order_from_frontend_open_order(
    db: Session,
    position: Position,
    order: Order,
    frontend_order: dict,
) -> None:
    old_oid = order.hyperliquid_order_id
    old_price = order.price
    trigger_px = _decimal(frontend_order.get("triggerPx"))
    synced_size = _decimal(frontend_order.get("sz")) or _decimal(frontend_order.get("origSz"))
    frontend_oid = frontend_order.get("oid")
    frontend_cloid = frontend_order.get("cloid")

    if trigger_px is not None:
        order.price = trigger_px
        if order.order_type == "stop_loss":
            position.stop_loss = trigger_px
            db.add(position)
    if synced_size is not None and synced_size > 0:
        order.size = synced_size
    if isinstance(frontend_oid, int):
        order.hyperliquid_order_id = str(frontend_oid)
    if isinstance(frontend_cloid, str) and frontend_cloid:
        order.cloid = frontend_cloid.lower().removeprefix("0x")
    order.status = "open"
    raw = dict(order.raw_response or {})
    raw["frontend_open_order_sync"] = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "old_oid": old_oid,
        "new_oid": order.hyperliquid_order_id,
        "old_price": str(old_price) if old_price is not None else None,
        "new_price": str(order.price) if order.price is not None else None,
        "frontend_order_type": frontend_order.get("orderType"),
    }
    order.raw_response = raw
    db.add(order)
    db.add(
        DecisionLog(
            event_type="live_protection_order_synced_from_frontend",
            market_id=position.market_id,
            position_id=position.id,
            message=(
                f"Protection order {order.order_type} synced from frontend open order: "
                f"price {order.price}, size {order.size}"
            ),
            data={
                "order_id": order.id,
                "order_type": order.order_type,
                "old_oid": old_oid,
                "new_oid": order.hyperliquid_order_id,
                "old_price": str(old_price) if old_price is not None else None,
                "new_price": str(order.price) if order.price is not None else None,
            },
        )
    )


def _violates_liquidation_guard(position: Position, stop_price: Decimal, remote_position: dict | None) -> bool:
    if order_side := position.side.lower():
        liquidation_price = _decimal(remote_position.get("liquidationPx")) if isinstance(remote_position, dict) else None
        if liquidation_price is None or liquidation_price <= 0:
            return False
        if order_side == "long":
            return stop_price <= liquidation_price
        if order_side == "short":
            return stop_price >= liquidation_price
    return False


def _resubmit_missing_protection_orders(
    db: Session,
    position: Position,
    coin: str,
    hl_open_oids: set[int],
    hl_open_cloids: set[str],
    frontend_orders: list[dict],
    trading: HyperliquidTradingClient,
    expires_after_seconds: int,
    sz_decimals: int,
    remote_position: dict | None = None,
    remote_size: Decimal | None = None,
) -> None:
    """Detect protection orders that have disappeared from HL and re-submit them.

    An order is considered missing when its recorded oid (and cloid) are absent
    from the live HL open-orders list AND the order is still in a non-terminal
    state in our DB (i.e. we haven't already processed a fill for it).

    For TP orders the resubmitted size is derived from the actual HL position
    size (remote_size) multiplied by the allocation recorded in the TP plan,
    rather than the potentially stale order.size stored in the DB.  The SL is
    always resubmitted with the full remote_size.

    To avoid infinite resubmit loops when HL persistently rejects an order
    (e.g. trigger price already passed), we stop retrying after
    _MAX_RESUBMIT_ATTEMPTS consecutive failed submissions.
    """
    pending_orders = (
        db.query(Order)
        .filter(
            Order.position_id == position.id,
            Order.mode == OrderMode.LIVE.value,
            Order.order_type.in_(_RECONCILED_EXIT_TYPES),
            Order.status.notin_(_TERMINAL_ORDER_STATUSES),
        )
        .all()
    )
    if not pending_orders:
        return

    close_is_buy = position.side.lower() == "short"
    allocation_map = _tp_allocation_map(position)

    for order in pending_orders:
        if order.status in _TERMINAL_ORDER_STATUSES:
            continue

        raw = dict(order.raw_response or {})
        resubmits: list[dict] = raw.get("resubmits") or []
        failed_attempts = sum(1 for r in resubmits if r.get("status") != "ok")
        if failed_attempts >= _MAX_RESUBMIT_ATTEMPTS:
            continue

        oid_int: int | None = None
        if order.hyperliquid_order_id:
            try:
                oid_int = int(order.hyperliquid_order_id)
            except (TypeError, ValueError):
                pass

        normalised_cloid = (order.cloid or "").lower().removeprefix("0x")

        if (oid_int is not None and oid_int in hl_open_oids) or (
            normalised_cloid and normalised_cloid in hl_open_cloids
        ):
            continue  # order is still open on HL — nothing to do

        frontend_match = _match_frontend_protection_order(frontend_orders, order, position)
        if frontend_match is not None:
            _sync_order_from_frontend_open_order(db, position, order, frontend_match)
            continue

        if order.price is None:
            continue

        tpsl = _TPSL_BY_ORDER_TYPE.get(order.order_type)
        if tpsl is None:
            continue

        if order.order_type == "stop_loss" and _violates_liquidation_guard(
            position,
            order.price,
            remote_position,
        ):
            raw["resubmits"] = [
                *resubmits,
                {
                    "old_oid": order.hyperliquid_order_id,
                    "status": "skipped_liquidation_guard",
                    "trigger_price": str(order.price),
                    "liquidation_price": str(_decimal(remote_position.get("liquidationPx")))
                    if isinstance(remote_position, dict)
                    else None,
                },
            ]
            order.raw_response = raw
            db.add(order)
            db.add(
                DecisionLog(
                    event_type="live_protection_order_resubmit_skipped_liquidation_guard",
                    market_id=position.market_id,
                    position_id=position.id,
                    message=(
                        f"Protection order {order.order_type} re-submit skipped: "
                        "trigger is not before liquidation price"
                    ),
                    data={
                        "coin": coin,
                        "order_id": order.id,
                        "order_type": order.order_type,
                        "trigger_price": str(order.price),
                        "liquidation_price": str(_decimal(remote_position.get("liquidationPx")))
                        if isinstance(remote_position, dict)
                        else None,
                    },
                )
            )
            logger.warning(
                "live_protection_order_resubmit_skipped_liquidation_guard",
                position_id=position.id,
                order_id=order.id,
                order_type=order.order_type,
                trigger_price=str(order.price),
            )
            continue

        # Compute the correct submission size:
        # - TP: remote_size * allocation (keeps the sum of all TPs = position size)
        # - SL: remote_size (always covers the full remaining position)
        # Fall back to the stored order.size only when remote_size is unavailable.
        if remote_size is not None and remote_size > 0:
            if order.order_type in ("tp1", "tp2", "tp3"):
                alloc = allocation_map.get(order.order_type)
                if alloc is not None and alloc > 0:
                    submit_size = _round_size(remote_size * alloc, sz_decimals)
                else:
                    submit_size = _round_size(order.size, sz_decimals)
            else:
                submit_size = _round_size(remote_size, sz_decimals)
        else:
            submit_size = _round_size(order.size, sz_decimals)

        if submit_size <= 0:
            logger.warning(
                "live_protection_order_resubmit_skip_zero_size",
                position_id=position.id,
                order_id=order.id,
                order_type=order.order_type,
                remote_size=str(remote_size),
            )
            continue

        logger.warning(
            "live_protection_order_missing_from_hl",
            position_id=position.id,
            order_id=order.id,
            order_type=order.order_type,
            coin=coin,
            old_oid=oid_int,
            submit_size=str(submit_size),
        )

        try:
            result = trading.submit_trigger_order(
                coin=coin,
                is_buy=close_is_buy,
                size=submit_size,
                trigger_price=order.price,
                tpsl=tpsl,
                expires_after_seconds=expires_after_seconds,
                cloid=None,  # no cloid: avoids HL duplicate-cloid rejection
            )
            old_oid = order.hyperliquid_order_id
            resubmit_entry: dict = {
                "old_oid": old_oid,
                "new_oid": result.oid,
                "status": result.status,
                "size": str(submit_size),
                "response": result.raw_response,
            }
            resubmits.append(resubmit_entry)
            raw["resubmits"] = resubmits

            if result.status != "ok":
                order.raw_response = raw
                db.add(order)
                db.add(
                    DecisionLog(
                        event_type="live_protection_order_resubmit_rejected",
                        market_id=position.market_id,
                        position_id=position.id,
                        message=f"Protection order {order.order_type} re-submission rejected by HL",
                        data={
                            "coin": coin,
                            "order_id": order.id,
                            "order_type": order.order_type,
                            "old_oid": old_oid,
                            "status": result.status,
                            "size": str(submit_size),
                            "response": result.raw_response,
                        },
                    )
                )
                logger.warning(
                    "live_protection_order_resubmit_rejected",
                    position_id=position.id,
                    order_id=order.id,
                    order_type=order.order_type,
                    status=result.status,
                )
                continue

            immediately_filled = result.filled_size is not None
            order.hyperliquid_order_id = result.oid
            order.status = "filled" if immediately_filled else "open"
            order.size = submit_size
            order.raw_response = raw
            db.add(order)
            if immediately_filled:
                db.add(
                    DecisionLog(
                        event_type="live_protection_order_resubmit_filled_immediately",
                        market_id=position.market_id,
                        position_id=position.id,
                        message=f"Protection order {order.order_type} re-submitted and immediately filled: price {order.price}, size {submit_size}",
                        data={
                            "coin": coin,
                            "order_id": order.id,
                            "order_type": order.order_type,
                            "old_oid": old_oid,
                            "new_oid": result.oid,
                            "avg_price": str(result.avg_price),
                            "filled_size": str(result.filled_size),
                            "size": str(submit_size),
                        },
                    )
                )
            else:
                db.add(
                    DecisionLog(
                        event_type="live_protection_order_resubmitted",
                        market_id=position.market_id,
                        position_id=position.id,
                        message=f"Protection order {order.order_type} re-submitted: price {order.price}, size {submit_size}",
                        data={
                            "coin": coin,
                            "order_id": order.id,
                            "order_type": order.order_type,
                            "old_oid": old_oid,
                            "new_oid": result.oid,
                            "status": result.status,
                            "size": str(submit_size),
                        },
                    )
                )
        except Exception as exc:
            resubmits.append(
                {"old_oid": order.hyperliquid_order_id, "status": "error", "error": str(exc)}
            )
            raw["resubmits"] = resubmits
            order.raw_response = raw
            db.add(order)
            logger.warning(
                "live_protection_order_resubmit_failed",
                position_id=position.id,
                order_id=order.id,
                order_type=order.order_type,
                error=str(exc),
            )


def _resize_local_stop_orders(
    db: Session,
    position: Position,
    remote_size: Decimal,
) -> None:
    pending_stops = (
        db.query(Order)
        .filter(
            Order.position_id == position.id,
            Order.mode == OrderMode.LIVE.value,
            Order.order_type == "stop_loss",
            Order.status.notin_(_TERMINAL_ORDER_STATUSES),
        )
        .all()
    )
    for order in pending_stops:
        if order.size == remote_size:
            continue
        previous_size = order.size
        order.size = remote_size
        raw = dict(order.raw_response or {})
        raw["local_size_adjusted_after_reconciliation"] = {
            "old_size": str(previous_size),
            "new_size": str(remote_size),
        }
        order.raw_response = raw
        db.add(order)
        db.add(
            DecisionLog(
                event_type="live_stop_order_size_reconciled",
                market_id=position.market_id,
                position_id=position.id,
                message=f"Stop order size aligned to HL position - from {previous_size} to {remote_size}",
                data={
                    "order_id": order.id,
                    "old_size": str(previous_size),
                    "new_size": str(remote_size),
                },
            )
        )


def _close_companion_paper_position(
    db: Session,
    live_position: Position,
    exit_price: Decimal | None,
    now: datetime,
) -> None:
    """If a companion paper position exists for the same signal, close it at exit_price."""
    paper_position = _companion_paper_position(db, live_position)
    if paper_position is None:
        return
    signal_id = _source_signal_id(live_position)
    if signal_id is None:
        return
    effective_exit = exit_price if exit_price is not None else paper_position.entry_price
    side = paper_position.side.lower()
    pnl_sign = Decimal("1") if side == "long" else Decimal("-1")
    realized_pnl = (effective_exit - paper_position.entry_price) * paper_position.size * pnl_sign
    paper_metadata = dict(paper_position.metadata_json or {})
    paper_metadata.update(
        {
            "exit_price": str(effective_exit),
            "exit_reason": "live_reconciliation_companion_close",
            "realized_pnl": str(realized_pnl),
            "remaining_size": "0",
        }
    )
    paper_position.status = "closed"
    paper_position.closed_at = now
    paper_position.size = Decimal("0")
    paper_position.realized_pnl = realized_pnl
    paper_position.unrealized_pnl = Decimal("0")
    paper_position.metadata_json = paper_metadata
    signal = db.get(Signal, signal_id)
    if signal is not None and signal.status != SignalStatus.CLOSED.value:
        signal.status = SignalStatus.CLOSED.value
        db.add(signal)
    db.add(paper_position)
    db.add(
        DecisionLog(
            event_type="paper_position_companion_closed",
            market_id=paper_position.market_id,
            position_id=paper_position.id,
            message="Paper position closed because companion live position was reconciled as closed",
            data={
                "live_position_id": live_position.id,
                "exit_price": str(effective_exit),
                "realized_pnl": str(realized_pnl),
            },
        )
    )


def _handle_companion_paper_after_live_reconcile(
    db: Session,
    live_position: Position,
    exit_price: Decimal | None,
    now: datetime,
    *,
    close_companion: bool,
) -> None:
    if close_companion:
        _close_companion_paper_position(db, live_position, exit_price, now)
        return

    paper_position = _companion_paper_position(db, live_position)
    if paper_position is None:
        return
    db.add(
        DecisionLog(
            event_type="live_companion_paper_left_open",
            market_id=paper_position.market_id,
            position_id=paper_position.id,
            message="Paper companion left open after live position reconciliation",
            data={
                "live_position_id": live_position.id,
                "live_exit_price": str(exit_price) if exit_price is not None else None,
                "source_signal_id": (live_position.metadata_json or {}).get("source_signal_id"),
            },
        )
    )


def _companion_paper_position(db: Session, live_position: Position) -> Position | None:
    signal_id = _source_signal_id(live_position)
    if signal_id is None:
        return None
    return (
        db.query(Position)
        .filter(
            Position.market_id == live_position.market_id,
            Position.mode == OrderMode.PAPER.value,
            Position.status == "open",
        )
        .filter(
            Position.metadata_json["source_signal_id"].astext == str(signal_id)
        )
        .first()
    )


def _source_signal_id(live_position: Position) -> int | None:
    source_signal_id = (live_position.metadata_json or {}).get("source_signal_id")
    if source_signal_id is None:
        return None
    try:
        return int(source_signal_id)
    except (TypeError, ValueError):
        return None


def _index_fills_by_coin(raw_fills: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for fill in raw_fills:
        if not isinstance(fill, dict):
            continue
        coin = fill.get("coin")
        if isinstance(coin, str) and coin:
            result.setdefault(coin, []).append(fill)
    return result


def _find_close_fill(
    coin: str,
    opened_at: datetime,
    fills_by_coin: dict[str, list[dict]],
) -> dict | None:
    """Return the most recent HL fill for this coin that carries a closedPnl
    (i.e. it's a position-close fill) and happened after the position opened.

    Using the real HL fill gives the exact avg price and the true P&L
    (including funding adjustments), so this is more accurate than deriving
    P&L from a trigger price stored in Trado's DB.
    """
    opened_ts = int(opened_at.timestamp() * 1000)
    best: dict | None = None
    best_time = -1
    for fill in fills_by_coin.get(coin, []):
        pnl = _decimal(fill.get("closedPnl"))
        if pnl is None or pnl == 0:
            continue
        fill_time = fill.get("time")
        if not isinstance(fill_time, int) or fill_time <= opened_ts:
            continue
        if fill_time > best_time:
            best = fill
            best_time = fill_time
    return best


def reconcile_live_positions(
    db: Session,
    trading_client: HyperliquidTradingClient | None = None,
) -> int:
    positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.mode == OrderMode.LIVE.value, Position.status == "open")
        .all()
    )
    if not positions:
        return 0

    settings = get_settings()
    if not settings.hyperliquid_account_address:
        return 0
    trading = trading_client or HyperliquidTradingClient(settings)
    remote_positions = _position_map(trading.user_state(settings.hyperliquid_account_address))
    try:
        raw_fills = trading.user_fills(settings.hyperliquid_account_address)
    except Exception as exc:
        # Fills are an enrichment — never fail the whole reconciliation if HL
        # returns an error on the userFills endpoint.
        logger.warning("live_reconciliation_user_fills_failed", error=str(exc))
        raw_fills = []
    fills_by_cloid, fills_by_oid = _index_fills(raw_fills or [])
    fills_by_coin = _index_fills_by_coin(raw_fills or [])

    open_orders_available = True
    try:
        raw_open_orders = trading.open_orders(settings.hyperliquid_account_address)
    except Exception as exc:
        logger.warning("live_reconciliation_open_orders_failed", error=str(exc))
        raw_open_orders = []
        open_orders_available = False
    hl_open_oids, hl_open_cloids = _index_open_orders(raw_open_orders)
    try:
        raw_frontend_open_orders = trading.frontend_open_orders(settings.hyperliquid_account_address)
    except Exception as exc:
        logger.warning("live_reconciliation_frontend_open_orders_failed", error=str(exc))
        raw_frontend_open_orders = []
    frontend_open_orders_by_coin = _index_frontend_open_orders(raw_frontend_open_orders)

    live_settings = effective_live_execution_settings(db, settings)
    expires_after_seconds = int(live_settings["order_expires_after_seconds"])

    updated = 0
    now = datetime.now(timezone.utc)
    for position in positions:
        if position.market is None:
            continue
        coin = _coin_for_market(position.market)
        remote = remote_positions.get(coin)
        metadata = dict(position.metadata_json or {})
        position.metadata_json = metadata
        fill_match = _reconcile_exit_fills(
            db, position, coin, fills_by_cloid, fills_by_oid
        )
        realized_delta = fill_match.realized_delta
        if realized_delta != 0:
            position.realized_pnl = (
                Decimal(position.realized_pnl or 0) + realized_delta
            )
            metadata["realized_pnl"] = str(position.realized_pnl)
        if remote is None:
            original_size = position.size
            position.status = "closed"
            position.closed_at = now
            position.size = Decimal("0")
            metadata["reconciled_closed_at"] = now.isoformat()

            if not metadata.get("exit_price") and fill_match.last_exit_price is not None:
                metadata["exit_price"] = str(fill_match.last_exit_price)
                if fill_match.last_exit_reason is not None:
                    metadata["exit_reason"] = fill_match.last_exit_reason

            if realized_delta == 0 and position.realized_pnl is None:
                # Fill not matched via cloid/oid. Strategy (highest accuracy first):
                # 1. Search the already-fetched HL fills for a close fill on this coin
                #    — gives exact avg price + true P&L (includes funding/fees).
                # 2. Fall back to the most recent pending SL/TP order's trigger price.
                hl_fill = _find_close_fill(coin, position.opened_at, fills_by_coin)
                if hl_fill is not None:
                    hl_px = _decimal(hl_fill.get("px"))
                    hl_pnl = _decimal(hl_fill.get("closedPnl"))
                    if hl_px is not None and hl_pnl is not None:
                        position.realized_pnl = hl_pnl
                        metadata["realized_pnl"] = str(hl_pnl)
                        metadata["exit_price"] = str(hl_px)
                        metadata["exit_reason"] = "hl_fill"
                        db.add(
                            DecisionLog(
                                event_type="live_reconciliation_hl_fill_fallback",
                                market_id=position.market_id,
                                position_id=position.id,
                                message="Close fill found in HL fill history; P&L and exit price taken from HL",
                                data={
                                    "coin": coin,
                                    "exit_price": str(hl_px),
                                    "realized_pnl": str(hl_pnl),
                                    "fill": hl_fill,
                                },
                            )
                        )
                else:
                    # No HL fill found: fall back to pending SL/TP trigger price
                    pending_exit = (
                        db.query(Order)
                        .filter(
                            Order.position_id == position.id,
                            Order.mode == OrderMode.LIVE.value,
                            Order.order_type.in_(_RECONCILED_EXIT_TYPES),
                            Order.status.notin_(_TERMINAL_ORDER_STATUSES),
                        )
                        .order_by(Order.created_at.desc())
                        .first()
                    )
                    if pending_exit is not None and pending_exit.price is not None:
                        exit_sz = pending_exit.size or original_size
                        pnl_sign = Decimal("1") if position.side.lower() == "long" else Decimal("-1")
                        est_pnl = (pending_exit.price - position.entry_price) * exit_sz * pnl_sign
                        position.realized_pnl = est_pnl
                        metadata["realized_pnl"] = str(est_pnl)
                        metadata["exit_price"] = str(pending_exit.price)
                        metadata["exit_reason"] = pending_exit.order_type
                        pending_exit.status = "filled"
                        db.add(pending_exit)
                        db.add(
                            DecisionLog(
                                event_type="live_reconciliation_sl_fallback",
                                market_id=position.market_id,
                                position_id=position.id,
                                message="SL/TP fill not in HL history; P&L estimated from trigger price",
                                data={
                                    "coin": coin,
                                    "order_id": pending_exit.id,
                                    "exit_price": str(pending_exit.price),
                                    "exit_size": str(exit_sz),
                                    "realized_pnl": str(est_pnl),
                                },
                            )
                        )

            if not metadata.get("exit_price"):
                # Fill was matched (realized_delta != 0) but exit_price not stored:
                # derive it from the most recently filled exit order's avg price.
                last_filled = (
                    db.query(Order)
                    .filter(
                        Order.position_id == position.id,
                        Order.mode == OrderMode.LIVE.value,
                        Order.order_type.in_(_RECONCILED_EXIT_TYPES),
                        Order.status == "filled",
                    )
                    .order_by(Order.created_at.desc())
                    .first()
                )
                if last_filled is not None and last_filled.price is not None:
                    metadata["exit_price"] = str(last_filled.price)

            position.metadata_json = metadata
            db.add(position)
            db.add(
                DecisionLog(
                    event_type="live_position_reconciled_closed",
                    market_id=position.market_id,
                    position_id=position.id,
                    message="Live position no longer present on Hyperliquid",
                    data={"coin": coin},
                )
            )
            exit_price_str = metadata.get("exit_price")
            companion_exit = _decimal(exit_price_str) if exit_price_str else None
            _handle_companion_paper_after_live_reconcile(
                db,
                position,
                companion_exit,
                now,
                close_companion=bool(live_settings["close_companion_paper_on_live_reconcile"]),
            )
            updated += 1
            continue

        # Compute remote_abs early so _resubmit_missing_protection_orders can
        # use the actual HL position size to derive correct TP/SL sizes.
        remote_size_raw = _decimal(remote.get("szi"))
        remote_abs: Decimal | None = abs(remote_size_raw) if remote_size_raw is not None else None

        if open_orders_available:
            _resubmit_missing_protection_orders(
                db,
                position,
                coin,
                hl_open_oids,
                hl_open_cloids,
                frontend_open_orders_by_coin.get(coin, []),
                trading,
                expires_after_seconds,
                _sz_decimals(position.market),
                remote_position=remote,
                remote_size=remote_abs,
            )

        remote_size = remote_size_raw
        if remote_size is not None:
            remote_abs = abs(remote_size)
            local_size = position.size or Decimal("0")
            divergence = _size_divergence(local_size, remote_abs)
            prev_mismatch = metadata.get("size_mismatch_at")
            # If we just reconciled exit fills, the local/remote size delta is
            # explained by those fills — apply remote_size directly without
            # raising a mismatch warning.
            if realized_delta != 0:
                divergence = Decimal("0")
            if divergence > SIZE_DIVERGENCE_TOLERANCE and prev_mismatch is None:
                # First time we see this divergence: log + skip the overwrite. If
                # next tick still shows it we apply, so a single-bad-payload
                # spike cannot silently corrupt local state.
                logger.warning(
                    "live_position_size_mismatch",
                    coin=coin,
                    local_size=str(local_size),
                    remote_size=str(remote_abs),
                    divergence=str(divergence),
                )
                metadata["size_mismatch_at"] = now.isoformat()
                metadata["size_mismatch_local"] = str(local_size)
                metadata["size_mismatch_remote"] = str(remote_abs)
                position.metadata_json = metadata
                db.add(position)
                db.add(
                    DecisionLog(
                        event_type="live_reconciliation_size_mismatch",
                        market_id=position.market_id,
                        position_id=position.id,
                        message="Local vs remote size diverge beyond tolerance",
                        data={
                            "coin": coin,
                            "local_size": str(local_size),
                            "remote_size": str(remote_abs),
                            "divergence": str(divergence),
                        },
                    )
                )
                updated += 1
                continue
            position.size = remote_abs
            _resize_local_stop_orders(db, position, remote_abs)
            position.side = "long" if remote_size > 0 else "short"
            metadata.pop("size_mismatch_at", None)
            metadata.pop("size_mismatch_local", None)
            metadata.pop("size_mismatch_remote", None)
        entry = _decimal(remote.get("entryPx"))
        if entry is not None:
            position.entry_price = entry
        unrealized = _decimal(remote.get("unrealizedPnl"))
        if unrealized is not None:
            position.unrealized_pnl = unrealized
            position.pnl_updated_at = now
        # mark_px: only trust HL's own markPx — never derive from unrealized/size,
        # because HL's unrealized includes fee/funding adjustments and the
        # derivation produces a price-equivalent that is off by those.
        mark_px = _decimal(remote.get("markPx"))
        if mark_px is not None:
            metadata["last_mark_price"] = str(mark_px)
        else:
            metadata.pop("last_mark_price", None)
        remote_lev = remote.get("leverage") or {}
        contract_leverage = remote_lev.get("value") if isinstance(remote_lev, dict) else None
        if contract_leverage is not None:
            metadata["contract_leverage"] = contract_leverage
        metadata["last_reconciled_at"] = now.isoformat()
        metadata["last_remote_position"] = remote
        position.metadata_json = metadata
        _protect_stop_at_breakeven_after_tp1(
            db,
            position,
            position.market,
            remote,
            fill_match,
            trading,
        )
        _protect_stop_lock_profit_after_tp2(
            db,
            position,
            position.market,
            remote,
            fill_match,
            trading,
        )
        db.add(position)
        updated += 1

    if updated:
        db.commit()
    return updated
