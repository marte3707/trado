from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.enums import OrderMode
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

        realized_delta += total_pnl
        db.add(
            DecisionLog(
                event_type=f"live_reconciliation_{order.order_type}_filled",
                market_id=position.market_id,
                position_id=position.id,
                message=f"Reconciled live {order.order_type} fill from Hyperliquid",
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
            position.status = "closed"
            position.closed_at = now
            position.size = Decimal("0")
            metadata["reconciled_closed_at"] = now.isoformat()
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

        remote_size = _decimal(remote.get("szi"))
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
