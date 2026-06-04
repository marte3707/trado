"""Update routing for the onboarding bot.

`handle_update` is the only entry point: dispatches based on update type and
content. Conversation state is read from the BotUser row, not from memory, so
the bot can be restarted at any time without losing flow state.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.bot import storage
from app.bot.i18n import normalize_language, t
from app.bot.telegram_api import (
    TelegramAPIError,
    TelegramBotAPI,
    disclaimer_keyboard,
    language_keyboard,
    trade_capital_keyboard,
)
from app.core.config import Settings

logger = structlog.get_logger()

_VALID_TRADE_CAPITAL = {"under_1k", "1k_to_10k", "over_10k"}


async def handle_update(
    api: TelegramBotAPI, settings: Settings, db: Session, update: dict[str, Any]
) -> None:
    if "message" in update:
        await _handle_message(api, settings, db, update["message"])
    elif "callback_query" in update:
        await _handle_callback(api, settings, db, update["callback_query"])


async def _handle_message(
    api: TelegramBotAPI, settings: Settings, db: Session, message: dict[str, Any]
) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None or chat.get("type") != "private":
        # Only handle DMs. Anything posted in groups/channels is ignored.
        return

    from_user = message.get("from") or {}
    telegram_user_id = from_user.get("id")
    if telegram_user_id is None:
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        # Free-text messages outside of commands: gently push them toward /help.
        # Use whatever language we already know about the user, otherwise EN.
        user = (
            db.query(__import__("app.db.models", fromlist=["BotUser"]).BotUser)
            .filter_by(telegram_user_id=telegram_user_id)
            .one_or_none()
        )
        lang = user.language if user else "en"
        await api.send_message(chat_id, t("unknown_command", lang))
        return

    # Strip @botname suffix Telegram appends in groups (harmless in DMs too).
    command, _, rest = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    args = rest.strip()

    if command == "/start":
        await _cmd_start(api, settings, db, chat_id, from_user, args)
    elif command == "/help":
        await _cmd_help(api, db, chat_id, telegram_user_id)
    elif command == "/disclaimer":
        await _cmd_disclaimer(api, db, chat_id, telegram_user_id)
    elif command == "/privacy":
        await _cmd_privacy(api, db, chat_id, telegram_user_id)
    elif command == "/pause":
        await _cmd_pause(api, db, chat_id, telegram_user_id, paused=True)
    elif command == "/resume":
        await _cmd_pause(api, db, chat_id, telegram_user_id, paused=False)
    elif command == "/lang":
        await _cmd_lang(api, db, chat_id, telegram_user_id)
    else:
        lang = _lookup_language(db, telegram_user_id)
        await api.send_message(chat_id, t("unknown_command", lang))


async def _cmd_start(
    api: TelegramBotAPI,
    settings: Settings,
    db: Session,
    chat_id: int,
    from_user: dict[str, Any],
    args: str,
) -> None:
    telegram_user_id = from_user["id"]
    username = from_user.get("username")
    first_name = from_user.get("first_name")

    # Source attribution: deep link parameter (`?start=landing`) lands here as
    # the command argument. Empty string means no attribution.
    source = args[:64] if args else None

    # Seed language from Telegram's own user.language_code if we know it. The
    # user can override via the inline keyboard immediately after.
    user = storage.get_or_create(
        db,
        telegram_user_id=telegram_user_id,
        telegram_username=username,
        first_name=first_name,
        source=source,
    )
    if user.language not in {"it", "en"}:
        seeded = normalize_language(from_user.get("language_code"))
        user.language = seeded
        db.commit()

    await api.send_message(
        chat_id,
        t("language_prompt", user.language),
        reply_markup=language_keyboard(),
    )


async def _cmd_help(api: TelegramBotAPI, db: Session, chat_id: int, telegram_user_id: int) -> None:
    lang = _lookup_language(db, telegram_user_id)
    await api.send_message(chat_id, t("help", lang))


async def _cmd_disclaimer(
    api: TelegramBotAPI, db: Session, chat_id: int, telegram_user_id: int
) -> None:
    lang = _lookup_language(db, telegram_user_id)
    await api.send_message(chat_id, t("disclaimer_prompt", lang))


async def _cmd_privacy(
    api: TelegramBotAPI, db: Session, chat_id: int, telegram_user_id: int
) -> None:
    lang = _lookup_language(db, telegram_user_id)
    await api.send_message(chat_id, t("privacy", lang))


async def _cmd_pause(
    api: TelegramBotAPI,
    db: Session,
    chat_id: int,
    telegram_user_id: int,
    *,
    paused: bool,
) -> None:
    from app.db.models import BotUser

    user = db.query(BotUser).filter_by(telegram_user_id=telegram_user_id).one_or_none()
    if user is None:
        await api.send_message(chat_id, t("start_again_hint", "en"))
        return
    storage.set_paused(db, user, paused)
    await api.send_message(chat_id, t("paused" if paused else "resumed", user.language))


async def _cmd_lang(api: TelegramBotAPI, db: Session, chat_id: int, telegram_user_id: int) -> None:
    lang = _lookup_language(db, telegram_user_id)
    await api.send_message(chat_id, t("lang_prompt", lang), reply_markup=language_keyboard())


async def _handle_callback(
    api: TelegramBotAPI, settings: Settings, db: Session, query: dict[str, Any]
) -> None:
    from app.db.models import BotUser

    query_id = query["id"]
    data = query.get("data") or ""
    from_user = query.get("from") or {}
    telegram_user_id = from_user.get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if telegram_user_id is None or chat_id is None:
        await api.answer_callback_query(query_id)
        return

    user = db.query(BotUser).filter_by(telegram_user_id=telegram_user_id).one_or_none()
    if user is None:
        await api.answer_callback_query(query_id)
        await api.send_message(chat_id, t("start_again_hint", "en"))
        return

    if data.startswith("lang:"):
        await _on_language_chosen(api, db, user, chat_id, message_id, data.split(":", 1)[1])
    elif data.startswith("cap:"):
        await _on_trade_capital_chosen(api, db, user, chat_id, message_id, data.split(":", 1)[1])
    elif data == "ack:accept":
        await _on_accept(api, settings, db, user, chat_id, message_id)
    elif data == "ack:decline":
        await _on_decline(api, db, user, chat_id, message_id)

    await api.answer_callback_query(query_id)


async def _on_language_chosen(
    api: TelegramBotAPI,
    db: Session,
    user: Any,
    chat_id: int,
    message_id: int | None,
    chosen: str,
) -> None:
    user = storage.set_language(db, user, chosen)
    if message_id is not None:
        await api.edit_message_text(chat_id, message_id, t("language_set", user.language))
    await api.send_message(
        chat_id,
        t("trade_capital_prompt", user.language),
        reply_markup=trade_capital_keyboard(user.language),
    )


async def _on_trade_capital_chosen(
    api: TelegramBotAPI,
    db: Session,
    user: Any,
    chat_id: int,
    message_id: int | None,
    chosen: str,
) -> None:
    # "skip" = the user declined to answer. Anything else must match a known
    # range; unexpected values are silently dropped to keep the flow tight.
    if chosen == "skip":
        value: str | None = None
    elif chosen in _VALID_TRADE_CAPITAL:
        value = chosen
    else:
        return
    user = storage.set_trade_capital(db, user, value)
    if message_id is not None:
        # Acknowledge selection by stripping the keyboard from the previous msg.
        await api.edit_message_text(
            chat_id, message_id, t("trade_capital_prompt", user.language)
        )
    await api.send_message(
        chat_id,
        t("disclaimer_prompt", user.language),
        reply_markup=disclaimer_keyboard(user.language),
    )


async def _on_accept(
    api: TelegramBotAPI,
    settings: Settings,
    db: Session,
    user: Any,
    chat_id: int,
    message_id: int | None,
) -> None:
    invite_link: str | None = None
    if settings.telegram_signal_chat_id:
        try:
            invite_link = await api.create_chat_invite_link(
                settings.telegram_signal_chat_id,
                name=f"tg-{user.telegram_user_id}-{(user.source or 'na')[:16]}",
            )
        except TelegramAPIError as exc:
            logger.warning(
                "telegram_invite_link_failed",
                user_id=user.telegram_user_id,
                error=str(exc),
            )
    else:
        logger.warning("telegram_signal_chat_id_not_configured")

    user = storage.record_risk_ack(db, user, invite_link=invite_link)

    if message_id is not None:
        await api.edit_message_text(
            chat_id, message_id, t("disclaimer_prompt", user.language)
        )

    if invite_link:
        await api.send_message(
            chat_id, t("invite_message", user.language).format(invite_link=invite_link)
        )
    else:
        await api.send_message(chat_id, t("invite_failed", user.language))


async def _on_decline(
    api: TelegramBotAPI,
    db: Session,
    user: Any,
    chat_id: int,
    message_id: int | None,
) -> None:
    user = storage.record_decline(db, user)
    if message_id is not None:
        await api.edit_message_text(
            chat_id, message_id, t("disclaimer_prompt", user.language)
        )
    await api.send_message(chat_id, t("declined", user.language))


def _lookup_language(db: Session, telegram_user_id: int) -> str:
    from app.db.models import BotUser

    user = db.query(BotUser).filter_by(telegram_user_id=telegram_user_id).one_or_none()
    return user.language if user else "en"
