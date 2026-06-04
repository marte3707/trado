"""Thin async wrapper around the Telegram Bot HTTP API.

Uses httpx (already pinned). No third-party bot framework, no new dependencies.
Only the methods needed by the onboarding flow are exposed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

_API_BASE = "https://api.telegram.org"


class TelegramAPIError(Exception):
    pass


class TelegramBotAPI:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self._token = token
        self._client = client

    def _url(self, method: str) -> str:
        return f"{_API_BASE}/bot{self._token}/{method}"

    async def _call(self, method: str, *, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        try:
            response = await self._client.post(self._url(method), json=params or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"http error calling {method}: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramAPIError(f"non-json response from {method}: {response.text[:200]}") from exc
        if not data.get("ok"):
            raise TelegramAPIError(f"telegram error on {method}: {data.get('description')}")
        return data["result"]

    async def get_updates(self, *, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        params = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        result = await self._call("getUpdates", params=params, timeout=timeout + 5)
        return list(result or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self._call("sendMessage", params=params)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            return await self._call("editMessageText", params=params)
        except TelegramAPIError as exc:
            # "message is not modified" and similar non-fatal cases — caller
            # already wrote what it wanted, just log and move on.
            logger.warning("telegram_edit_failed", error=str(exc))
            return None

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        try:
            await self._call("answerCallbackQuery", params=params)
        except TelegramAPIError as exc:
            logger.warning("telegram_answer_callback_failed", error=str(exc))

    async def create_chat_invite_link(
        self,
        chat_id: int | str,
        *,
        name: str | None = None,
        expire_seconds: int = 24 * 3600,
        member_limit: int = 1,
    ) -> str:
        expire_date = int((datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)).timestamp())
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "expire_date": expire_date,
            "member_limit": member_limit,
            "creates_join_request": False,
        }
        if name:
            params["name"] = name[:32]  # Telegram caps name at 32 chars
        result = await self._call("createChatInviteLink", params=params)
        return result["invite_link"]

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        await self._call("setMyCommands", params={"commands": commands})


def language_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🇮🇹 Italiano", "callback_data": "lang:it"},
                {"text": "🇬🇧 English", "callback_data": "lang:en"},
            ]
        ]
    }


def trade_capital_keyboard(language: str) -> dict[str, Any]:
    from app.bot.i18n import t

    return {
        "inline_keyboard": [
            [{"text": t("btn_cap_under_1k", language), "callback_data": "cap:under_1k"}],
            [{"text": t("btn_cap_1k_to_10k", language), "callback_data": "cap:1k_to_10k"}],
            [{"text": t("btn_cap_over_10k", language), "callback_data": "cap:over_10k"}],
            [{"text": t("btn_skip", language), "callback_data": "cap:skip"}],
        ]
    }


def disclaimer_keyboard(language: str) -> dict[str, Any]:
    from app.bot.i18n import t

    return {
        "inline_keyboard": [
            [{"text": t("btn_accept", language), "callback_data": "ack:accept"}],
            [{"text": t("btn_decline", language), "callback_data": "ack:decline"}],
        ]
    }
