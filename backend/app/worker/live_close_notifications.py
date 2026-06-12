"""Notifiche Telegram per chiusure live, reversal BTC e stato macro risk."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.runtime_settings import effective_macro_risk_monitor_settings
from app.db.models import AppSetting, DecisionLog, Position
from app.notifications.telegram import TelegramNotifier

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.

logger = structlog.get_logger()
_FILL_EVENT_TYPES = frozenset(
    {
        "live_reconciliation_tp1_filled",
        "live_reconciliation_tp2_filled",
        "live_reconciliation_tp3_filled",
        "live_reconciliation_stop_loss_filled",
    }
)
_MACRO_RISK_LEVEL_ORDER = {"off": 0, "watch": 1, "high": 2, "extreme": 3, "manual": 4}


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


async def _notify_macro_risk_state_change(
    db: Session,
    notifier: TelegramNotifier,
    result: dict[str, Any],
) -> None:
    if not result.get("changed"):
        return
    state = result.get("state")
    if not isinstance(state, dict):
        return
    previous = result.get("previous_state")
    previous_state = previous if isinstance(previous, dict) else {}
    config = effective_macro_risk_monitor_settings(db)
    notify, transition = _should_notify_macro_risk_state(previous_state, state, config)
    if not notify:
        return
    try:
        await notifier.send_macro_risk_state_changed(state)
        _persist_macro_risk_notification_state(db, state, transition)
    except Exception as exc:
        logger.warning("telegram_macro_risk_notify_failed", error=str(exc))


def _macro_risk_transition(previous: dict[str, Any], state: dict[str, Any]) -> str:
    previous_active = bool(previous.get("active"))
    current_active = bool(state.get("active"))
    previous_level = str(previous.get("level") or "off").lower()
    current_level = str(state.get("level") or "off").lower()

    if not previous_active and current_active:
        return "activation"
    if previous_active and not current_active:
        return "recovery"
    if not previous_active and not current_active:
        return "noop"

    previous_order = _MACRO_RISK_LEVEL_ORDER.get(previous_level, 0)
    current_order = _MACRO_RISK_LEVEL_ORDER.get(current_level, 0)
    if current_order > previous_order:
        return "escalation"
    if current_order < previous_order:
        return "downgrade"
    return "refresh"


def _should_notify_macro_risk_state(
    previous: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any],
) -> tuple[bool, str]:
    transition = _macro_risk_transition(previous, state)
    if transition in {"noop", "refresh"}:
        return False, transition
    if transition == "downgrade" and not bool(config.get("telegram_notify_on_downgrade", False)):
        return False, transition
    if transition == "recovery":
        return True, transition

    cooldown_minutes = max(0, int(config.get("telegram_notify_cooldown_minutes", 180) or 0))
    if cooldown_minutes <= 0:
        return True, transition

    last_sent_at = _parse_dt(state.get("last_telegram_risk_notification_at"))
    if last_sent_at is None:
        return True, transition
    current_at = _parse_dt(state.get("last_checked_at")) or datetime.now(UTC)
    if current_at - last_sent_at >= timedelta(minutes=cooldown_minutes):
        return True, transition

    last_level = str(state.get("last_telegram_risk_notification_level") or "off").lower()
    current_level = str(state.get("level") or "off").lower()
    if _MACRO_RISK_LEVEL_ORDER.get(current_level, 0) > _MACRO_RISK_LEVEL_ORDER.get(last_level, 0):
        return True, transition
    return False, transition


def _persist_macro_risk_notification_state(
    db: Session,
    state: dict[str, Any],
    transition: str,
) -> None:
    if transition == "recovery":
        return
    row = db.get(AppSetting, "macro_risk_state")
    if row is None or not isinstance(row.value, dict):
        return
    next_state = dict(row.value)
    next_state["last_telegram_risk_notification_at"] = (
        _parse_dt(state.get("last_checked_at")) or datetime.now(UTC)
    ).isoformat()
    next_state["last_telegram_risk_notification_level"] = state.get("level")
    row.value = next_state
    state.update(
        {
            "last_telegram_risk_notification_at": next_state["last_telegram_risk_notification_at"],
            "last_telegram_risk_notification_level": next_state["last_telegram_risk_notification_level"],
        }
    )
    db.commit()


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
