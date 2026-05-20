from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.enums import OrderMode
from app.core.runtime_settings import effective_live_execution_settings
from app.db.models import DecisionLog, Market, Order, Position
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
) -> Decimal:
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
        return Decimal("0")

    metadata = position.metadata_json or {}
    plan = metadata.get("take_profit_plan")
    plan_mut = plan if isinstance(plan, list) else None
    realized_delta = Decimal("0")

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

    return realized_delta


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


def _resubmit_missing_protection_orders(
    db: Session,
    position: Position,
    coin: str,
    hl_open_oids: set[int],
    hl_open_cloids: set[str],
    trading: HyperliquidTradingClient,
    expires_after_seconds: int,
    sz_decimals: int,
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

        if order.price is None:
            continue

        tpsl = _TPSL_BY_ORDER_TYPE.get(order.order_type)
        if tpsl is None:
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
        realized_delta = _reconcile_exit_fills(
            db, position, coin, fills_by_cloid, fills_by_oid
        )
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

            if "exit_price" not in metadata:
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
                trading,
                expires_after_seconds,
                _sz_decimals(position.market),
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
        db.add(position)
        updated += 1

    if updated:
        db.commit()
    return updated
