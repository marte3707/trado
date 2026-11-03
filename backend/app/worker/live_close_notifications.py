"""Notifiche Telegram per chiusure live e reversal BTC."""
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy.orm import Session

from app.db.models import DecisionLog, Position
from app.notifications.telegram import TelegramNotifier

logger = structlog.get_logger()
_FILL_EVENT_TYPES = frozenset(
    {
        "live_reconciliation_tp1_filled",
        "live_reconciliation_tp2_filled",
        "live_reconciliation_tp3_filled",
        "live_reconciliation_stop_loss_filled",
    }
)
def _is_profitable_close(
    position: "Position",
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> bool:
    if pnl_usd is not None:
        return pnl_usd > 0
    if exit_price is None:
        return False
    side = position.side.lower()
    if side == "long":
        return exit_price > position.entry_price
    if side == "short":
        return exit_price < position.entry_price
    return False


def _display_live_close_reason(
    position: "Position",
    reason: str,
    *,
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> str:
    if reason.lower() != "stop_loss":
        return reason
    if _is_profitable_close(position, exit_price, pnl_usd):
        return "trailing_stop"
    return reason


def _calc_close_card_extras(
    position: "Position",
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> tuple[float | None, int | None]:
    """Restituisce (pnl_pct, duration_seconds) per la share card."""
    pnl_pct: float | None = None
    # Usa la leva contrattuale HL (es. 10x) se disponibile, altrimenti nessun %
    meta = position.metadata_json or {}
    contract_lev = meta.get("contract_leverage")
    lev = float(contract_lev) if contract_lev is not None else None

    if lev and lev > 0:
        if exit_price is not None:
            ep = float(position.entry_price)
            xp = float(exit_price)
            if ep > 0:
                direction = 1 if position.side.upper() == "LONG" else -1
                pnl_pct = direction * (xp - ep) / ep * lev * 100
        elif pnl_usd is not None and position.size:
            # fallback: stima da USD se non abbiamo exit price
            notional = float(position.entry_price) * float(position.size)
            if notional > 0:
                pnl_pct = float(pnl_usd) / notional * lev * 100

    duration: int | None = None
    if position.opened_at:
        ref = position.closed_at or datetime.now(UTC)
        opened = position.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        duration = int((ref - opened).total_seconds())

    return pnl_pct, duration


async def _notify_live_position_closes(
    db: Session,
    notifier: TelegramNotifier,
    since: datetime,
) -> None:
    events = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type.in_(
                [
                    *_FILL_EVENT_TYPES,
                    "position_closed",
                    "position_partially_closed",
                    "live_position_reconciled_closed",
                ]
            ),
            DecisionLog.timestamp >= since,
            DecisionLog.position_id.isnot(None),
        )
        .order_by(DecisionLog.timestamp)
        .all()
    )
    if not events:
        return

    positions_with_fills: set[int] = {
        e.position_id
        for e in events
        if e.event_type in _FILL_EVENT_TYPES and e.position_id is not None
    }

    for event in events:
        if event.position_id is None:
            continue
        position = db.get(Position, event.position_id)
        if position is None:
            continue
        market = position.market
        if market is None:
            continue

        position_meta = position.metadata_json or {}
        raw_source_signal = position_meta.get("source_signal_id")
        try:
            source_signal_id = int(raw_source_signal) if raw_source_signal is not None else None
        except (TypeError, ValueError):
            source_signal_id = None

        try:
            if event.event_type in _FILL_EVENT_TYPES:
                order_type = (
                    event.event_type.replace("live_reconciliation_", "").replace("_filled", "")
                )
                exit_price_str = event.data.get("avg_price")
                exit_price = Decimal(exit_price_str) if exit_price_str else None
                step_pnl_str = event.data.get("closed_pnl")
                step_pnl = Decimal(step_pnl_str) if step_pnl_str else None
                is_partial = position.status == "open"
                remaining_size = position.size if is_partial else None
                total_pnl = (
                    Decimal(str(position.realized_pnl or 0)) if not is_partial else None
                )
                pnl_usd = step_pnl if is_partial else total_pnl
                display_reason = _display_live_close_reason(
                    position,
                    order_type,
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                )
                pnl_pct, duration = _calc_close_card_extras(position, exit_price, pnl_usd)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason=display_reason,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size,
                    step_pnl=step_pnl,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

            elif event.event_type in ("position_closed", "position_partially_closed"):
                if event.data.get("mode") != "live":
                    continue
                exit_price_str = event.data.get("exit_price")
                exit_price = Decimal(exit_price_str) if exit_price_str else None
                step_pnl_str = event.data.get("realized_pnl")
                step_pnl = Decimal(step_pnl_str) if step_pnl_str else None
                remaining_str = event.data.get("remaining_size")
                remaining_size = Decimal(remaining_str) if remaining_str else None
                is_partial = event.event_type == "position_partially_closed"
                cum_str = event.data.get("cumulative_realized_pnl")
                total_pnl = Decimal(cum_str) if cum_str and not is_partial else None
                reason = event.data.get("reason") or "manual"
                pnl_usd = step_pnl if is_partial else total_pnl
                display_reason = _display_live_close_reason(
                    position,
                    reason,
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                )
                pnl_pct, duration = _calc_close_card_extras(position, exit_price, pnl_usd)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=event.data.get("side") or position.side,
                    reason=display_reason,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size if is_partial else None,
                    step_pnl=step_pnl if is_partial else None,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

            elif event.event_type == "live_position_reconciled_closed":
                if event.position_id in positions_with_fills:
                    continue
                recon_pnl = Decimal(str(position.realized_pnl or 0))
                pnl_pct, duration = _calc_close_card_extras(position, None, recon_pnl)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason="reconciliazione hl",
                    entry_price=position.entry_price,
                    exit_price=None,
                    remaining_size=None,
                    step_pnl=None,
                    total_pnl=recon_pnl,
                    is_partial=False,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

        except Exception:
            logger.warning(
                "telegram_live_close_notify_failed",
                event_type=event.event_type,
                position_id=event.position_id,
            )


async def _notify_stop_update_failures(
    db: Session,
    notifier: TelegramNotifier,
    since: datetime,
) -> None:
    """Alert immediato (admin) sui fallimenti di aggiornamento stop live: subito
    per le mosse safety-critical (breakeven/profitto) e per gli stop 'stuck'
    (>=3 tentativi). I trailing di routine restano fuori per non fare rumore."""
    events = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type.in_(
                ["live_stop_update_submit_failed", "live_stop_update_stuck"]
            ),
            DecisionLog.timestamp >= since,
            DecisionLog.position_id.isnot(None),
        )
        .order_by(DecisionLog.timestamp)
        .all()
    )
    if not events:
        return
    stuck_positions: set[int] = {
        e.position_id
        for e in events
        if e.event_type == "live_stop_update_stuck" and e.position_id is not None
    }
    notified: set[int] = set()
    for event in events:
        position_id = event.position_id
        if position_id is None or position_id in notified:
            continue
        data = event.data or {}
        is_stuck = event.event_type == "live_stop_update_stuck"
        # Submit-failed non-stuck: notifica solo se safety-critical e non gia'
        # coperto dall'evento 'stuck' della stessa posizione in questo tick.
        if not is_stuck and (not data.get("safety_critical") or position_id in stuck_positions):
            continue
        position = db.get(Position, position_id)
        if position is None or position.market is None:
            continue
        raw_detail = data.get("error") or data.get("response")
        error_detail = None if raw_detail is None else str(raw_detail)
        try:
            await notifier.send_stop_update_failure(
                symbol=position.market.symbol,
                side=position.side,
                reason=str(data.get("reason") or "—"),
                candidate_stop=data.get("candidate_stop"),
                attempts=int(data.get("attempts") or 1),
                error=error_detail,
                stuck=is_stuck,
            )
            notified.add(position_id)
        except Exception:
            logger.warning(
                "telegram_stop_update_failure_notify_failed",
                position_id=position_id,
            )


async def _notify_btc_macro_reversals(
    db: Session,
    notifier: TelegramNotifier,
    since: datetime,
) -> None:
    """Broadcast BTC macro reversal events logged during the current tick to
    the public signals channel. One Telegram message per detected reversal."""
    events = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type == "btc_macro_reversal_detected",
            DecisionLog.timestamp >= since,
        )
        .order_by(DecisionLog.timestamp)
        .all()
    )
    if not events:
        return
    for event in events:
        data = event.data or {}
        try:
            await notifier.send_btc_macro_detected(
                from_trend=str(data.get("from_trend") or "—"),
                to_trend=str(data.get("to_trend") or "—"),
                delta_pct=float(data.get("delta_pct") or 0.0),
                vol_ratio=float(data.get("vol_ratio") or 0.0),
                timeframe=str(data.get("timeframe") or "—"),
            )
        except Exception:
            logger.warning(
                "telegram_btc_macro_notify_failed",
                event_id=event.id,
            )
