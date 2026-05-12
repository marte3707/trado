from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, Signal


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_signal(self, db: Session, signal: Signal) -> None:
        payload = {
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
        text = (
            f"Signal {signal.direction} score {signal.score}\n"
            f"Entry: {signal.entry_price}\n"
            f"SL: {signal.stop_loss}\n"
            f"TP1: {signal.take_profit_1}\n"
            f"TP2: {signal.take_profit_2}\n"
            f"R/R: {signal.risk_reward}"
        )
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={"chat_id": self.settings.telegram_chat_id, "text": text},
            )
        alert.status = "sent" if response.is_success else "failed"
        alert.sent_at = datetime.now(timezone.utc) if response.is_success else None
        alert.payload = {**payload, "telegram_status": response.status_code}
        db.commit()
