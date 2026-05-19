from datetime import datetime, timezone
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, AppSetting, Position, Signal

logger = structlog.get_logger()

_DIRECTION_ICON = {"LONG": "📈", "SHORT": "📉"}
_SCORE_FILLED = "●"
_SCORE_EMPTY = "○"
_SCORE_STEPS = 10

_REASON_LABELS: dict[str, str] = {
    "tp1": "TP1",
    "tp2": "TP2",
    "tp3": "TP3",
    "take_profit": "Take Profit",
    "stop_loss": "Stop Loss",
    "manual": "Manuale",
    "btc_macro_protection_loss_exit": "BTC Macro Stop",
    "btc_macro_protection_breakeven": "BTC Macro Breakeven",
    "reconciliazione hl": "Riconciliata HL",
}


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    normalized = value.normalize()
    # keep at least 2 decimal places for readability
    if normalized == normalized.to_integral_value():
        return f"{normalized:,.2f}"
    return f"{normalized:,f}"


def _score_bar(score: int) -> str:
    filled = round(score / 100 * _SCORE_STEPS)
    return _SCORE_FILLED * filled + _SCORE_EMPTY * (_SCORE_STEPS - filled)


def _fmt_reason(reason: str) -> str:
    return _REASON_LABELS.get(reason.lower(), reason.upper())


def _build_live_close_message(
    *,
    symbol: str,
    side: str,
    reason: str,
    entry_price: Decimal,
    exit_price: Decimal | None,
    remaining_size: Decimal | None,
    step_pnl: Decimal | None,
    total_pnl: Decimal | None,
    is_partial: bool,
) -> str:
    direction = side.upper()
    side_icon = _DIRECTION_ICON.get(direction, "🔔")
    sep = "━━━━━━━━━━━━━━━━━━━━"

    if is_partial:
        header_icon = "🟡"
        header = f"{header_icon} <b>LIVE · Chiusura parziale</b>"
        pnl_label = "PnL"
        pnl_value = step_pnl
    else:
        pnl_value = total_pnl
        if pnl_value is not None and float(pnl_value) >= 0:
            header_icon = "✅"
        else:
            header_icon = "🔴"
        header = f"{header_icon} <b>LIVE · Posizione chiusa</b>"
        pnl_label = "PnL tot"

    lines = [
        header,
        f"{side_icon} {symbol} · {direction}",
        sep,
        f"📊 Motivo:  <code>{_fmt_reason(reason)}</code>",
        f"💰 Entry:   <code>{_fmt_price(entry_price)}</code>",
    ]
    if exit_price is not None:
        lines.append(f"📤 Exit:    <code>{_fmt_price(exit_price)}</code>")
    if is_partial and remaining_size is not None:
        lines.append(f"📦 Rimasto: <code>{remaining_size.normalize():f}</code>")
    if pnl_value is not None:
        sign = "+" if float(pnl_value) >= 0 else ""
        pnl_icon = "📈" if float(pnl_value) >= 0 else "📉"
        lines.append(f"{pnl_icon} {pnl_label}: <code>{sign}{float(pnl_value):.2f} $</code>")
    lines += [sep, "🤖 <i>Trado · Hyperliquid</i>"]
    return "\n".join(lines)


def _build_message(signal: Signal) -> str:
    symbol = signal.market.symbol if signal.market else "—"
    direction = signal.direction.upper()
    icon = _DIRECTION_ICON.get(direction, "🔔")
    score_bar = _score_bar(signal.score)
    sep = "━━━━━━━━━━━━━━━━━━━━"

    lines = [
        f"{icon} <b>{symbol} · {direction}</b>  |  {signal.timeframe}",
        sep,
        f"⭐ Score  <b>{signal.score}/100</b>  <code>{score_bar}</code>",
        "",
        f"💰 Entry       <code>{_fmt_price(signal.entry_price)}</code>",
    ]

    if signal.stop_loss is not None:
        lines.append(f"🛑 Stop Loss   <code>{_fmt_price(signal.stop_loss)}</code>")

    if signal.take_profit_1 is not None:
        lines.append(f"🎯 TP1         <code>{_fmt_price(signal.take_profit_1)}</code>")
    if signal.take_profit_2 is not None:
        lines.append(f"🎯 TP2         <code>{_fmt_price(signal.take_profit_2)}</code>")
    if signal.take_profit_3 is not None:
        lines.append(f"🎯 TP3         <code>{_fmt_price(signal.take_profit_3)}</code>")

    if signal.risk_reward is not None:
        lines.append(f"⚖️ R/R          <code>{signal.risk_reward:.2f}</code>")

    lines += [sep, "🤖 <i>Trado · Hyperliquid</i>"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _send_text(self, text: str) -> bool:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.settings.telegram_chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
            return response.is_success
        except Exception as exc:
            logger.warning("telegram_send_failed", error=str(exc))
            return False

    async def send_worker_online(self, db: Session) -> None:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        mode = "—"
        score = "—"
        defensive = False
        try:
            mode_setting = db.get(AppSetting, "operation_mode")
            if mode_setting:
                mode = mode_setting.value.get("mode", "—")
            risk_setting = db.get(AppSetting, "risk")
            if risk_setting:
                score = risk_setting.value.get("min_signal_score", "—")
            cal_state = db.get(AppSetting, "auto_calibration_state")
            if cal_state:
                defensive = bool(cal_state.value.get("deep_defensive_active", False))
        except Exception:
            pass
        defensive_line = "\n⚠️ <b>Modalità difensiva attiva</b>" if defensive else ""
        await self._send_text(
            f"✅ <b>Trado Worker ONLINE</b>\n"
            f"<code>{now}</code>\n"
            f"\n⚙️ Config: modalità <b>{mode}</b> · score min <b>{score}</b>"
            f"{defensive_line}"
        )

    async def send_worker_error(self, context: str, exc: Exception) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        error_type = type(exc).__name__
        error_msg = str(exc)[:300]
        await self._send_text(
            f"🔴 <b>Trado Worker — Errore</b>\n"
            f"<code>{now}</code>\n\n"
            f"Contesto: <code>{context}</code>\n"
            f"<code>{error_type}: {error_msg}</code>"
        )

    async def send_hourly_recap(self, db: Session, stats: dict) -> None:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M UTC")

        # posizioni aperte
        try:
            paper_count = (
                db.query(func.count(Position.id))
                .filter(Position.mode == "paper", Position.status == "open")
                .scalar() or 0
            )
            paper_pnl = (
                db.query(func.sum(Position.unrealized_pnl))
                .filter(Position.mode == "paper", Position.status == "open")
                .scalar()
            )
            live_count = (
                db.query(func.count(Position.id))
                .filter(Position.mode == "live", Position.status == "open")
                .scalar() or 0
            )
            live_pnl = (
                db.query(func.sum(Position.unrealized_pnl))
                .filter(Position.mode == "live", Position.status == "open")
                .scalar()
            )
        except Exception:
            paper_count = live_count = 0
            paper_pnl = live_pnl = None

        # impostazioni correnti
        mode = "—"
        score = "—"
        defensive = False
        try:
            mode_setting = db.get(AppSetting, "operation_mode")
            if mode_setting:
                mode = mode_setting.value.get("mode", "—")
            risk_setting = db.get(AppSetting, "risk")
            if risk_setting:
                score = risk_setting.value.get("min_signal_score", "—")
            cal_state = db.get(AppSetting, "auto_calibration_state")
            if cal_state:
                defensive = bool(cal_state.value.get("deep_defensive_active", False))
        except Exception:
            pass

        def _pnl(v) -> str:
            if v is None:
                return "n/d"
            sign = "+" if float(v) >= 0 else ""
            return f"{sign}{float(v):.2f} $"

        cycles = stats.get("cycles", 0)
        signals = stats.get("signals_generated", 0)
        candle_errors = stats.get("candle_errors", 0)
        cycle_failures = stats.get("cycle_failures", 0)
        period_min = stats.get("period_minutes", 60)

        defensive_line = "\n⚠️ <b>Modalità difensiva attiva</b>" if defensive else ""
        positions_block = (
            f"\n\n📌 <b>Posizioni aperte</b>\n"
            f"  Paper: {paper_count} | PnL: <code>{_pnl(paper_pnl)}</code>\n"
            f"  Live:  {live_count} | PnL: <code>{_pnl(live_pnl)}</code>"
        )

        await self._send_text(
            f"📊 <b>Trado — Recap {period_min} min</b>\n"
            f"<code>{now_str}</code>\n\n"
            f"📡 Cicli: {cycles} | Errori ciclo: {cycle_failures}\n"
            f"📈 Segnali generati: {signals}\n"
            f"⚠️ Errori candle sync: {candle_errors}"
            f"{positions_block}\n\n"
            f"⚙️ Modalità: <b>{mode}</b> · Score min: <b>{score}</b>"
            f"{defensive_line}"
        )

    async def send_live_position_closed(
        self,
        *,
        symbol: str,
        side: str,
        reason: str,
        entry_price: Decimal,
        exit_price: Decimal | None,
        remaining_size: Decimal | None,
        step_pnl: Decimal | None,
        total_pnl: Decimal | None,
        is_partial: bool,
    ) -> None:
        text = _build_live_close_message(
            symbol=symbol,
            side=side,
            reason=reason,
            entry_price=entry_price,
            exit_price=exit_price,
            remaining_size=remaining_size,
            step_pnl=step_pnl,
            total_pnl=total_pnl,
            is_partial=is_partial,
        )
        await self._send_text(text)

    async def send_signal(self, db: Session, signal: Signal) -> None:
        payload: dict[str, str | int | None] = {
            "signal_id": signal.id,
            "direction": signal.direction,
            "score": signal.score,
            "entry": str(signal.entry_price),
            "stop_loss": str(signal.stop_loss),
            "take_profit_1": str(signal.take_profit_1),
            "take_profit_2": str(signal.take_profit_2),
            "take_profit_3": str(signal.take_profit_3) if signal.take_profit_3 is not None else None,
            "risk_reward": str(signal.risk_reward),
        }
        alert = Alert(signal_id=signal.id, channel="telegram", status="pending", payload=payload)
        db.add(alert)
        db.commit()
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            alert.status = "skipped"
            db.commit()
            return
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": _build_message(signal),
                    "parse_mode": "HTML",
                },
            )
        alert.status = "sent" if response.is_success else "failed"
        alert.sent_at = datetime.now(timezone.utc) if response.is_success else None
        alert.payload = {**payload, "telegram_status": response.status_code}
        db.commit()
