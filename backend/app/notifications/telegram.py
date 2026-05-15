from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, Signal

_DIRECTION_ICON = {"LONG": "📈", "SHORT": "📉"}
_SCORE_FILLED = "●"
_SCORE_EMPTY = "○"
_SCORE_STEPS = 10


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
