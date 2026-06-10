"""Outbound notifications from the bot process to the *admin* Telegram chat.

The onboarding bot uses the PUBLIC token for user-facing messages, but admin
health updates (startup, heartbeat, errors) need to land in the admin DM. Those
go via the admin bot token, regardless of which bot ran the onboarding flow.

This module owns that side channel so the rest of the bot code doesn't need
to know about the second token.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import BotUser

logger = structlog.get_logger()


async def _send_admin(settings: Settings, text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        return response.is_success
    except Exception as exc:
        logger.warning("admin_notify_send_failed", error=str(exc))
        return False


async def send_bot_online(settings: Settings, db: Session) -> None:
    now = datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC")
    total_users = db.query(func.count(BotUser.id)).scalar() or 0
    completed = (
        db.query(func.count(BotUser.id))
        .filter(BotUser.onboarding_stage == "completed")
        .scalar()
        or 0
    )
    username = settings.landing_bot_username or "—"
    using_dedicated = bool(settings.landing_bot_token)
    token_note = "token dedicato" if using_dedicated else "fallback admin token"
    await _send_admin(
        settings,
        f"🤖 <b>Trado Bot ONLINE</b>\n"
        f"<code>{now}</code>\n\n"
        f"Bot pubblico: <b>@{username.lstrip('@')}</b> ({token_note})\n"
        f"Utenti registrati: <b>{total_users}</b> · completati: <b>{completed}</b>",
    )


async def send_bot_heartbeat(
    settings: Settings,
    db: Session,
    *,
    updates_handled: int,
    errors: int,
    uptime_seconds: float,
) -> None:
    total_users = db.query(func.count(BotUser.id)).scalar() or 0
    completed = (
        db.query(func.count(BotUser.id))
        .filter(BotUser.onboarding_stage == "completed")
        .scalar()
        or 0
    )
    in_flow = (
        db.query(func.count(BotUser.id))
        .filter(BotUser.onboarding_stage.in_(("started", "language_set", "capital_set")))
        .scalar()
        or 0
    )
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    uptime_str = f"{hours}h {minutes}m"
    err_line = f"\n⚠️ Errori nel periodo: <b>{errors}</b>" if errors > 0 else ""
    await _send_admin(
        settings,
        f"💓 <b>Trado Bot — heartbeat</b>\n"
        f"Uptime: <code>{uptime_str}</code>\n\n"
        f"Update gestiti nel periodo: <b>{updates_handled}</b>\n"
        f"Utenti: <b>{total_users}</b> totali · "
        f"<b>{completed}</b> completati · <b>{in_flow}</b> in flow"
        f"{err_line}",
    )


async def send_bot_fatal(settings: Settings, context: str, exc: Exception) -> None:
    now = datetime.now(UTC).strftime("%H:%M UTC")
    error_type = type(exc).__name__
    error_msg = str(exc)[:300]
    await _send_admin(
        settings,
        f"🔴 <b>Trado Bot — errore fatale</b>\n"
        f"<code>{now}</code>\n\n"
        f"Contesto: <code>{context}</code>\n"
        f"<code>{error_type}: {error_msg}</code>",
    )
