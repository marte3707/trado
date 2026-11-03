from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import structlog
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, MarketType, OperationMode, OrderMode, SignalStatus
from app.core.runtime_settings import (
    DEFAULT_THE_BULL_SETTINGS,
    effective_operation_mode,
    setting_value,
)
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, Signal
from app.engine.execution import OrderExecutionEngine, coin_for_market
from app.engine.risk import RiskManager
from app.services.candle_sync import CandleSyncService
from app.services.hyperliquid import HyperliquidTradingClient
from app.strategies.base import CandleInput

logger = structlog.get_logger()

THE_BULL_SETTING_KEY = "the_bull"
THE_BULL_STATE_KEY = "the_bull_state"

_ENTRY_PATTERNS = (
    re.compile(r"\bbuy(?:ing)?\s+[$#]([A-Z0-9]{2,20})\b", re.IGNORECASE),
    re.compile(
        r"[$#]([A-Z0-9]{2,20})\s+(?:is\s+(?:a\s+)?buy|buy\s+setup)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b([A-Z0-9]{2,20}(?:USD|USDT|USDC))\s*-\s*BIDS?\s+A\s+LONG\b",
        re.IGNORECASE,
    ),
)
_AVERAGING_RE = re.compile(
    r"\b(?:add(?:ing)?|buy(?:ing)?)\s+more\b|"
    r"\b(?:add(?:ing)?|buy(?:ing)?)\s+[$#][A-Z0-9]{2,20}\s+more\b",
    re.IGNORECASE,
)
_AUDIT_ENTRY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "buy_zone",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:is\s+)?(?:currently\s+)?"
            r"(?:in\s+)?(?:a\s+)?buy\s+zone\b",
            re.IGNORECASE,
        ),
    ),
    (
        "buy_the_dip",
        re.compile(
            r"\bbuy\s+the\s+dip(?:\s+(?:on|for))?\s+[$#]([A-Z0-9]{2,20})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "buy_the_dip",
        re.compile(r"[$#]([A-Z0-9]{2,20})\s+buy\s+the\s+dip\b", re.IGNORECASE),
    ),
    (
        "buy_the_dip",
        re.compile(r"[$#]([A-Z0-9]{2,20})\s+dip\s+is\s+for\s+buying\b", re.IGNORECASE),
    ),
    (
        "support_buy",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:buying|adding|add)\s+"
            r"(?:here|at|on)\b.*\b(?:support|zone)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "support_buy",
        re.compile(
            r"\b(?:buying|adding|add)\s+[$#]([A-Z0-9]{2,20})\b"
            r".*\b(?:support|zone)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "averaging_add",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:buying|adding|add)\s+more\b",
            re.IGNORECASE,
        ),
    ),
    (
        "averaging_add",
        re.compile(
            r"\b(?:buying|adding|add)\s+more\s+[$#]([A-Z0-9]{2,20})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "averaging_add",
        re.compile(
            r"\b(?:buying|adding|add)\s+[$#]([A-Z0-9]{2,20})\s+more\b",
            re.IGNORECASE,
        ),
    ),
    (
        "breakout_retest",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:clear\s+)?break(?:-| )?out\s+"
            r"and\s+retest\s+(?:done|confirmed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "breakout_confirmed",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:clear\s+)?break(?:-| )?out\s+confirmed\b",
            re.IGNORECASE,
        ),
    ),
    (
        "breakout_active",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\b.{0,60}?\b(?:is\s+)?break(?:-| )?ing\s+out\b",
            re.IGNORECASE,
        ),
    ),
    (
        "breakout_call",
        re.compile(
            r"[$#]([A-Z0-9]{2,20})\s+(?:clear\s+)?break(?:-| )?out\b",
            re.IGNORECASE,
        ),
    ),
    (
        "double_bottom",
        re.compile(r"[$#]([A-Z0-9]{2,20})\s+double\s+bottom\b", re.IGNORECASE),
    ),
    (
        "next_leg_up",
        re.compile(r"[$#]([A-Z0-9]{2,20})\s+next\s+leg\s+up\b", re.IGNORECASE),
    ),
    (
        "bullish_bias",
        re.compile(r"[$#]([A-Z0-9]{2,20})\s+(?:very\s+)?bullish(?:\s+bias)?\b", re.IGNORECASE),
    ),
    (
        "watchlist_entry",
        re.compile(r"\bdo\s+not\s+miss\s+[$#]([A-Z0-9]{2,20})\b", re.IGNORECASE),
    ),
)
_TICKER_RE = re.compile(r"[$#]([A-Z0-9]{2,20})\b", re.IGNORECASE)
_TICKER_ONLY_CAPTION_RE = re.compile(r"^[$#]([A-Z0-9]{2,20})[.!]?$", re.IGNORECASE)
_HASHTAG_ONLY_TICKER_RE = re.compile(r"^#([A-Z0-9]{2,20})\b$", re.IGNORECASE)
_CONTINUATION_RE = re.compile(
    r"\b(?:just\s+getting\s+started|more\s+to\s+come|first\s+tp|tp\s*1)\b",
    re.IGNORECASE,
)
_STOP_EXIT_RE = re.compile(
    r"\b(?:stopped\s+out|stop(?:\s+loss)?\s+(?:was\s+)?hit|closed\s+at\s+stop)\b",
    re.IGNORECASE,
)
_EXPLICIT_CLOSE_RE = re.compile(
    r"\b(?:position|trade)?\s*(?:fully\s+)?(?:closed|sold)|"
    r"\bbook(?:ed|ing)\s+(?:all|the\s+rest|full|profit)|"
    r"\b(?:final|total)\s+(?:profit|pnl)\b",
    re.IGNORECASE,
)
_PROFIT_FROM_ENTRY_RE = re.compile(
    r"(?:\bup\s*)?[+]?[0-9]+(?:[.,][0-9]+)?\s*%\s+from\s+(?:our\s+)?entry\b",
    re.IGNORECASE,
)
_PERFORMANCE_UPDATE_RE = re.compile(
    r"(?<!\w)\+\s*[0-9]+(?:[.,][0-9]+)?\s*%|"
    r"\b(?:up|gained?|profit|pnl|roi)\s+(?:by\s+)?\+?[0-9]+(?:[.,][0-9]+)?\s*%|"
    r"\boutperform(?:s|ed|ing)?\b",
    re.IGNORECASE,
)
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD")
_MAX_TRACKED_MESSAGES = 250
_MAX_CHART_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_TELEGRAM_PREVIEW_PAGES = 5
_SPOT_HISTORY_LOOKBACK = 120
_MIN_SPOT_RISK_CANDLES = 30
_MAX_SPOT_HISTORY_BACKFILLS_PER_POLL = 2
_ALLOWED_MEDIA_HOST_RE = re.compile(r"(?:cdn\d+\.)?telesco\.pe", re.IGNORECASE)
_MEDIA_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")\s]+)", re.IGNORECASE)
_EXPANDED_EXECUTABLE_INTENTS = frozenset(
    {
        "buy_zone",
        "buy_the_dip",
        "averaging_add",
        "support_buy",
        "breakout_retest",
        "breakout_confirmed",
        "breakout_active",
        "breakout_call",
        "double_bottom",
        "next_leg_up",
        "bullish_bias",
        "watchlist_entry",
    }
)


@dataclass(frozen=True)
class EntryClassification:
    """Intent inferred from an external source message.

    Only pre-existing unambiguous call language is eligible by default; the
    caller may explicitly enable the supported broader vocabulary with
    ``expanded_entry_execution``.
    """

    symbol: str
    intent: str
    execution_eligible: bool


@dataclass(frozen=True)
class TelegramChannelMessage:
    channel_username: str
    message_id: int
    published_at: datetime
    text: str
    reply_to_message_id: int | None = None
    has_media: bool = False
    media_urls: tuple[str, ...] = ()
    external_urls: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel_username}/{self.message_id}"


class _TelegramPreviewHTMLParser(HTMLParser):
    """Small fail-closed parser for Telegram's public channel preview page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[TelegramChannelMessage] = []
        self._current: dict[str, Any] | None = None
        self._text_depth = 0
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        data_post = attr.get("data-post")
        if tag == "div" and "tgme_widget_message" in classes and data_post:
            self._finish_current()
            channel, _, raw_id = data_post.rpartition("/")
            try:
                message_id = int(raw_id)
            except ValueError:
                self._current = None
                return
            self._current = {
                "channel_username": channel,
                "message_id": message_id,
                "published_at": None,
                "reply_to_message_id": None,
                "text": "",
                "has_media": False,
                "media_urls": set(),
                "external_urls": set(),
            }
            return

        if self._current is None:
            return

        if tag == "a" and "tgme_widget_message_reply" in classes:
            reply_id = _message_id_from_url(attr.get("href", ""))
            if reply_id is not None:
                self._current["reply_to_message_id"] = reply_id
        elif tag == "a" and self._text_depth > 0:
            href = attr.get("href", "")
            if href.startswith(("https://", "http://")):
                self._current["external_urls"].add(href)

        # The channel avatar is an <img> inside every message, including plain
        # text posts. Only Telegram's attachment wrappers represent media.
        is_media = tag == "video" or bool(
            classes.intersection(
                {
                    "tgme_widget_message_photo_wrap",
                    "tgme_widget_message_video_player",
                    "tgme_widget_message_document_wrap",
                }
            )
        )
        if is_media:
            self._current["has_media"] = True
            self._capture_media_urls(attr)

        if tag == "time" and attr.get("datetime"):
            try:
                published_at = datetime.fromisoformat(attr["datetime"])
            except ValueError:
                published_at = None
            if published_at is not None:
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                self._current["published_at"] = published_at

        if (
            tag == "div"
            and "tgme_widget_message_text" in classes
            and "js-message_text" in classes
        ):
            self._text_depth = 1
            self._text_parts = []
        elif self._text_depth > 0 and tag not in {"br", "img", "meta", "link", "input"}:
            self._text_depth += 1

        if self._text_depth > 0 and tag == "br":
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._text_depth <= 0 or tag in {"br", "img", "meta", "link", "input"}:
            return
        self._text_depth -= 1
        if self._text_depth == 0 and self._current is not None:
            self._current["text"] = _normalize_message_text("".join(self._text_parts))
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._text_depth > 0:
            self._text_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is None:
            return
        published_at = self._current.get("published_at")
        if isinstance(published_at, datetime):
            self.messages.append(
                TelegramChannelMessage(
                    channel_username=str(self._current["channel_username"]),
                    message_id=int(self._current["message_id"]),
                    published_at=published_at,
                    text=str(self._current.get("text") or ""),
                    reply_to_message_id=self._current.get("reply_to_message_id"),
                    has_media=bool(self._current.get("has_media")),
                    media_urls=tuple(sorted(self._current.get("media_urls") or ())),
                    external_urls=tuple(sorted(self._current.get("external_urls") or ())),
                )
            )
        self._current = None

    def _capture_media_urls(self, attrs: dict[str, str]) -> None:
        if self._current is None:
            return
        candidates = [attrs.get("src", ""), attrs.get("data-original", ""), attrs.get("href", "")]
        candidates.extend(_MEDIA_URL_RE.findall(attrs.get("style", "")))
        for candidate in candidates:
            if _is_supported_media_url(candidate):
                self._current["media_urls"].add(candidate)


def parse_telegram_preview_html(payload: str) -> list[TelegramChannelMessage]:
    parser = _TelegramPreviewHTMLParser()
    parser.feed(payload)
    parser.close()
    by_id = {message.message_id: message for message in parser.messages}
    return [by_id[message_id] for message_id in sorted(by_id)]


def entry_symbol_from_message(text: str) -> str | None:
    classification = classify_entry_message(text)
    return classification.symbol if classification and classification.execution_eligible else None


def classify_entry_message(text: str) -> EntryClassification | None:
    """Classify explicit and broader entry language for either ticker marker."""
    normalized = _normalize_message_text(text)
    if not normalized:
        return None

    if not _AVERAGING_RE.search(normalized):
        for pattern in _ENTRY_PATTERNS:
            for match in pattern.finditer(normalized):
                symbol = _normalize_symbol(match.group(1))
                if not _is_ticker_symbol(symbol):
                    continue
                return EntryClassification(
                    symbol=symbol,
                    intent="explicit_buy",
                    execution_eligible=True,
                )

    for intent, pattern in _AUDIT_ENTRY_PATTERNS:
        for match in pattern.finditer(normalized):
            symbol = _normalize_symbol(match.group(1))
            if not _is_ticker_symbol(symbol):
                continue
            return EntryClassification(
                symbol=symbol,
                intent=intent,
                execution_eligible=False,
            )
    return None


def exit_reason_from_message(
    message: TelegramChannelMessage,
    *,
    close_on_profit_update: bool,
) -> str | None:
    if message.reply_to_message_id is None:
        return None
    text = _normalize_message_text(message.text)
    if not text:
        return None
    if _STOP_EXIT_RE.search(text):
        return "the_bull_stop_update"
    if _EXPLICIT_CLOSE_RE.search(text):
        return "the_bull_close_update"
    if (
        close_on_profit_update
        and not _CONTINUATION_RE.search(text)
        and _PROFIT_FROM_ENTRY_RE.search(text)
    ):
        return "the_bull_profit_update"
    return None


def _is_non_entry_performance_update(
    message: TelegramChannelMessage,
    entry: EntryClassification | None,
) -> bool:
    """A reported return is not a fresh entry merely because it has a chart/ticker."""
    if entry is not None and entry.execution_eligible:
        return False
    return bool(
        _ticker_symbol_from_message(message.text)
        and _PERFORMANCE_UPDATE_RE.search(_normalize_message_text(message.text))
    )


def effective_the_bull_settings(db: Session) -> dict[str, Any]:
    raw = setting_value(db, THE_BULL_SETTING_KEY)
    merged = {**DEFAULT_THE_BULL_SETTINGS, **raw}
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["close_on_profit_update"] = bool(merged.get("close_on_profit_update", True))
    merged["close_companion_paper_on_source_exit"] = bool(
        merged.get("close_companion_paper_on_source_exit", True)
    )
    merged["semantic_audit_enabled"] = bool(merged.get("semantic_audit_enabled", True))
    merged["expanded_entry_execution"] = bool(merged.get("expanded_entry_execution", True))
    merged["chart_analysis_enabled"] = bool(merged.get("chart_analysis_enabled", False))
    merged["chart_zone_execution"] = bool(merged.get("chart_zone_execution", True))
    merged["chart_ocr_fallback_execution"] = bool(
        merged.get("chart_ocr_fallback_execution", False)
    )
    merged["spot_fallback_enabled"] = bool(merged.get("spot_fallback_enabled", False))
    merged["channel_username"] = _safe_channel_username(merged.get("channel_username"))
    merged["execution_mode"] = (
        str(merged.get("execution_mode"))
        if str(merged.get("execution_mode")) in {"paper", "live"}
        else "paper"
    )
    merged["timeframe"] = (
        str(merged.get("timeframe"))
        if str(merged.get("timeframe")) in {"1h", "4h", "1d"}
        else "4h"
    )
    merged["feed_history_limit"] = _bounded_int(
        merged.get("feed_history_limit"), 25, 10, 100
    )
    merged["max_signal_age_minutes"] = _bounded_int(
        merged.get("max_signal_age_minutes"), 60, 1, 120
    )
    merged["signal_score"] = _bounded_int(merged.get("signal_score"), 60, 1, 100)
    merged["risk_multiplier"] = _bounded_float(
        merged.get("risk_multiplier"), 0.5, 0.05, 1.0
    )
    merged["max_open_positions"] = _bounded_int(
        merged.get("max_open_positions"), 3, 1, 20
    )
    merged["max_additions_per_ticker"] = _bounded_int(
        merged.get("max_additions_per_ticker"), 2, 0, 2
    )
    merged["max_addition_fraction"] = _bounded_float(
        merged.get("max_addition_fraction"), 0.30, 0.001, 0.30
    )
    merged["min_stop_distance_pct"] = _bounded_float(
        merged.get("min_stop_distance_pct"), 0.03, 0.005, 0.20
    )
    merged["chart_analysis_max_per_cycle"] = _bounded_int(
        merged.get("chart_analysis_max_per_cycle"), 2, 1, 5
    )
    return merged


class TheBullPublicFeed:
    def __init__(
        self,
        channel_username: str,
        timeout_seconds: float = 15.0,
        history_limit: int = 25,
    ) -> None:
        self.channel_username = _safe_channel_username(channel_username)
        self.timeout_seconds = timeout_seconds
        self.history_limit = _bounded_int(history_limit, 25, 10, 100)

    async def fetch_latest(self) -> list[TelegramChannelMessage]:
        """Fetch a bounded recent window from Telegram's paginated public preview."""
        url = f"https://t.me/s/{self.channel_username}"
        messages_by_id: dict[int, TelegramChannelMessage] = {}
        before_message_id: int | None = None
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Trado/1.0 public-signal-monitor"},
        ) as client:
            for _ in range(_MAX_TELEGRAM_PREVIEW_PAGES):
                params = {"before": before_message_id} if before_message_id is not None else None
                response = await client.get(url, params=params)
                response.raise_for_status()
                page_messages = parse_telegram_preview_html(response.text)
                if not page_messages:
                    break

                messages_by_id.update({message.message_id: message for message in page_messages})
                if len(messages_by_id) >= self.history_limit:
                    break

                oldest_message_id = min(message.message_id for message in page_messages)
                if (
                    before_message_id is not None
                    and oldest_message_id >= before_message_id
                ):
                    break
                before_message_id = oldest_message_id

        messages = [messages_by_id[message_id] for message_id in sorted(messages_by_id)]
        if not messages:
            raise RuntimeError("Telegram public preview returned no messages")
        return messages[-self.history_limit :]


async def poll_the_bull_signals(
    db: Session,
    settings: Settings,
    executor: OrderExecutionEngine,
) -> dict[str, Any]:
    config = effective_the_bull_settings(db)
    if not config["enabled"]:
        return {
            "status": "disabled",
            "processed": 0,
            "opened": 0,
            "added": 0,
            "closed": 0,
            "candidates": 0,
            "review_required": 0,
        }
    try:
        messages = await TheBullPublicFeed(
            config["channel_username"],
            history_limit=int(config["feed_history_limit"]),
        ).fetch_latest()
    except Exception as exc:
        _record_poll_error(db, config, exc)
        return {
            "status": "error",
            "processed": 0,
            "opened": 0,
            "added": 0,
            "closed": 0,
            "candidates": 0,
            "review_required": 0,
            "error": _safe_error(exc),
        }

    await _prefetch_new_spot_entry_history(db, messages, config)

    return process_the_bull_messages(
        db,
        settings,
        executor,
        messages,
        config=config,
    )


async def _prefetch_new_spot_entry_history(
    db: Session,
    messages: list[TelegramChannelMessage],
    config: dict[str, Any],
) -> None:
    """Backfill just enough spot history for a fresh The Bull fallback call.

    The normal worker universe is intentionally perpetual-only.  Rather than
    broadening it (and multiplying market-data traffic), a newly observed,
    source-eligible USDC spot call gets a bounded one-time candle backfill
    before its ordinary risk plan is evaluated.
    """
    if not bool(config["spot_fallback_enabled"]):
        return
    state_row = db.get(AppSetting, THE_BULL_STATE_KEY)
    state = dict(state_row.value) if state_row is not None and isinstance(state_row.value, dict) else {}
    cursor = _safe_int(state.get("last_message_id"))
    if cursor is None:
        return
    candidates: list[Market] = []
    candidate_ids: set[int] = set()
    for message in sorted(messages, key=lambda row: row.message_id):
        if message.message_id <= cursor:
            continue
        entry = classify_entry_message(message.text)
        symbol = entry.symbol if entry is not None else _hashtag_chart_entry_symbol(message)
        is_eligible = bool(
            entry is not None
            and (entry.execution_eligible or _can_execute_expanded_entry(entry, config))
        ) or bool(symbol is not None and config["expanded_entry_execution"])
        if not is_eligible or symbol is None:
            continue
        market = _active_the_bull_market(db, symbol, allow_spot_fallback=True)
        if (
            market is None
            or market.market_type != MarketType.SPOT.value
            or market.id in candidate_ids
        ):
            continue
        candidate_ids.add(market.id)
        candidates.append(market)
        if len(candidates) >= _MAX_SPOT_HISTORY_BACKFILLS_PER_POLL:
            break

    sync = CandleSyncService()
    timeframe = str(config["timeframe"])
    for market in candidates:
        candle_count = (
            db.query(Candle)
            .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
            .count()
        )
        if candle_count >= _MIN_SPOT_RISK_CANDLES:
            continue
        try:
            await sync.sync_market(
                db,
                market,
                timeframe,
                lookback=_SPOT_HISTORY_LOOKBACK,
                attempts=1,
            )
        except Exception as exc:
            logger.warning(
                "the_bull_spot_history_backfill_failed",
                market=market.symbol,
                timeframe=timeframe,
                error=_safe_error(exc),
            )


def process_the_bull_messages(
    db: Session,
    settings: Settings,
    executor: OrderExecutionEngine,
    messages: list[TelegramChannelMessage],
    *,
    config: dict[str, Any] | None = None,
    mids: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config or effective_the_bull_settings(db)
    now = now or datetime.now(UTC)
    ordered = sorted(messages, key=lambda message: message.message_id)
    state_row = db.get(AppSetting, THE_BULL_STATE_KEY)
    state = dict(state_row.value) if state_row is not None and isinstance(state_row.value, dict) else {}
    state["last_polled_at"] = now.isoformat()
    state["last_error"] = None

    if not ordered:
        _write_state(db, state_row, state)
        db.commit()
        return {
            "status": "empty",
            "processed": 0,
            "opened": 0,
            "closed": 0,
            "candidates": 0,
            "review_required": 0,
        }

    latest_id = ordered[-1].message_id
    cursor = _safe_int(state.get("last_message_id"))
    if cursor is None:
        state.update(
            {
                "last_message_id": latest_id,
                "initialized_at": now.isoformat(),
                "tracked_messages": {},
            }
        )
        _write_state(db, state_row, state)
        db.add(
            DecisionLog(
                event_type="the_bull_feed_initialized",
                message="The Bull feed initialized at the latest public message; history was not traded",
                data={
                    "channel_username": config["channel_username"],
                    "last_message_id": latest_id,
                },
            )
        )
        db.commit()
        return {
            "status": "initialized",
            "processed": 0,
            "opened": 0,
            "closed": 0,
            "candidates": 0,
            "review_required": 0,
            "last_message_id": latest_id,
        }

    new_messages = [message for message in ordered if message.message_id > cursor]
    result: dict[str, Any] = {
        "status": "ok",
        "processed": 0,
        "opened": 0,
        "added": 0,
        "closed": 0,
        "ignored": 0,
        "failed": 0,
        "candidates": 0,
        "review_required": 0,
        "chart_reviewed": 0,
        "chart_candidates": 0,
        "chart_failed": 0,
        "chart_ocr_fallbacks": 0,
    }
    tracked = dict(state.get("tracked_messages") or {})
    resolved_mids = mids
    chart_reviews = 0

    for message in new_messages:
        result["processed"] += 1
        try:
            chart_result: dict[str, Any] | None = None
            chart_message_entry = classify_entry_message(message.text)
            performance_update = _is_non_entry_performance_update(
                message, chart_message_entry
            )
            if (
                not performance_update
                and bool(config["chart_analysis_enabled"])
                and message.has_media
                and chart_reviews < int(config["chart_analysis_max_per_cycle"])
            ):
                chart_reviews += 1
                chart_result = analyze_the_bull_chart(settings, message, config)
                chart_execution = (
                    "eligible" if bool(config["chart_zone_execution"]) else "audit_only"
                )
                _record_chart_analysis(db, message, chart_result, now, chart_execution)
                if chart_result["status"] == "reviewed":
                    result["chart_reviewed"] += 1
                    if chart_result["has_actionable_buy_zone"]:
                        result["chart_candidates"] += 1
                        state["last_chart_candidate"] = _chart_candidate_state(
                            message, chart_result, now, chart_execution
                        )
                        if (
                            bool(config["chart_zone_execution"])
                            and not (
                                chart_message_entry is not None
                                and chart_message_entry.intent == "averaging_add"
                            )
                        ):
                            if resolved_mids is None:
                                resolved_mids = HyperliquidTradingClient(settings).all_mids(
                                    max_age_seconds=5
                                )
                            chart_signal, chart_opened = _handle_chart_zone_entry(
                                db,
                                settings,
                                executor,
                                message,
                                chart_result,
                                resolved_mids,
                                config,
                                now,
                            )
                            result["opened"] += chart_opened
                            if chart_signal is not None:
                                tracked[str(message.message_id)] = {
                                    "signal_id": chart_signal.id,
                                    "symbol": str(chart_result.get("symbol") or ""),
                                    "published_at": message.published_at.isoformat(),
                                }
                else:
                    result["chart_failed"] += 1

            close_reason = exit_reason_from_message(
                message,
                close_on_profit_update=bool(config["close_on_profit_update"]),
            )
            if close_reason is not None:
                _record_observed_message(
                    db,
                    message,
                    classification="exit_update",
                    execution="exit_handling",
                )
                if resolved_mids is None:
                    resolved_mids = HyperliquidTradingClient(settings).all_mids(
                        max_age_seconds=5
                    )
                closed = _handle_exit_message(
                    db,
                    executor,
                    message,
                    close_reason,
                    tracked,
                    resolved_mids,
                    config,
                )
                result["closed"] += closed
                if closed == 0:
                    result["ignored"] += 1
            elif performance_update:
                _record_observed_message(
                    db,
                    message,
                    classification="performance_update",
                    symbol=_ticker_symbol_from_message(message.text),
                    execution="ignored_non_entry",
                )
                result["ignored"] += 1
            else:
                entry = chart_message_entry
                hashtag_chart_symbol = _hashtag_chart_entry_symbol(message)
                chart_ocr_fallback_symbol = _chart_ocr_fallback_symbol(
                    message, chart_result, entry, config
                )
                expanded_entry = _can_execute_expanded_entry(entry, config)
                hashtag_chart_entry = bool(
                    hashtag_chart_symbol is not None and config["expanded_entry_execution"]
                )
                if (
                    entry is not None and (entry.execution_eligible or expanded_entry)
                ) or hashtag_chart_entry:
                    symbol = entry.symbol if entry is not None else hashtag_chart_symbol
                    intent = entry.intent if entry is not None else "tagged_chart_entry"
                    assert symbol is not None
                    _record_observed_message(
                        db,
                        message,
                        classification=intent,
                        symbol=symbol,
                        execution=(
                            "eligible"
                            if entry is not None and entry.execution_eligible
                            else "eligible_expanded"
                        ),
                    )
                    if resolved_mids is None:
                        resolved_mids = HyperliquidTradingClient(settings).all_mids(
                            max_age_seconds=5
                        )
                    signal, opened = _handle_entry_message(
                        db,
                        settings,
                        executor,
                        message,
                        symbol,
                        resolved_mids,
                        config,
                        now,
                        intent,
                    )
                    if signal is not None:
                        tracked[str(message.message_id)] = {
                            "signal_id": signal.id,
                            "symbol": symbol,
                            "published_at": message.published_at.isoformat(),
                        }
                    result["opened"] += opened
                    if (
                        signal is not None
                        and signal.status == SignalStatus.EXECUTED.value
                        and bool((signal.strategy_breakdown or {}).get("_external_source", {}).get("is_addition"))
                    ):
                        result["added"] += 1
                    if signal is None or signal.status == SignalStatus.IGNORED.value:
                        result["ignored"] += 1
                elif chart_ocr_fallback_symbol is not None:
                    _record_observed_message(
                        db,
                        message,
                        classification="chart_ocr_fallback",
                        symbol=chart_ocr_fallback_symbol,
                        execution="eligible_ocr_fallback",
                    )
                    if resolved_mids is None:
                        resolved_mids = HyperliquidTradingClient(settings).all_mids(
                            max_age_seconds=5
                        )
                    signal, opened = _handle_entry_message(
                        db,
                        settings,
                        executor,
                        message,
                        chart_ocr_fallback_symbol,
                        resolved_mids,
                        config,
                        now,
                        "chart_ocr_fallback",
                    )
                    result["chart_ocr_fallbacks"] += 1
                    result["opened"] += opened
                    if signal is not None:
                        tracked[str(message.message_id)] = {
                            "signal_id": signal.id,
                            "symbol": chart_ocr_fallback_symbol,
                            "published_at": message.published_at.isoformat(),
                        }
                    else:
                        result["ignored"] += 1
                elif entry is not None and bool(config["semantic_audit_enabled"]):
                    _record_audit_candidate(db, message, entry, now)
                    result["candidates"] += 1
                    state["last_candidate"] = _candidate_state(message, entry, now)
                elif _requires_context_review(message) and bool(config["semantic_audit_enabled"]):
                    symbol = _ticker_symbol_from_message(message.text)
                    _record_observed_message(
                        db,
                        message,
                        classification="context_review",
                        symbol=symbol,
                        execution="manual_review",
                    )
                    result["review_required"] += 1
                    state["last_candidate"] = _context_review_state(message, symbol, now)
                else:
                    _record_observed_message(
                        db,
                        message,
                        classification="neutral",
                        execution="not_applicable",
                    )
                    result["ignored"] += 1
        except Exception as exc:
            db.rollback()
            result["failed"] += 1
            db.add(
                DecisionLog(
                    event_type="the_bull_message_failed",
                    message="The Bull message processing failed",
                    data={
                        "message_id": message.message_id,
                        "message_url": message.url,
                        "error": _safe_error(exc),
                    },
                )
            )
            logger.exception(
                "the_bull_message_failed",
                message_id=message.message_id,
                error=_safe_error(exc),
            )

        state["last_message_id"] = message.message_id
        state["last_processed_at"] = now.isoformat()
        state["tracked_messages"] = _trim_tracked_messages(tracked)
        state_row = _write_state(db, state_row, state)
        db.commit()

    if not new_messages:
        state_row = _write_state(db, state_row, state)
        db.commit()

    result["last_message_id"] = int(state.get("last_message_id") or cursor)
    return result


def _record_observed_message(
    db: Session,
    message: TelegramChannelMessage,
    *,
    classification: str,
    execution: str,
    symbol: str | None = None,
) -> None:
    """Persist every observed public message, including non-trade commentary.

    Keeping the raw text makes source semantics reviewable after Telegram edits
    or removes a post. This is audit data only and has no execution effect.
    """

    market = _active_perpetual_market(db, symbol) if symbol else None
    db.add(
        DecisionLog(
            event_type="the_bull_message_observed",
            market_id=market.id if market is not None else None,
            message=f"The Bull message observed: {classification}",
            data={
                "message_id": message.message_id,
                "message_url": message.url,
                "published_at": message.published_at.isoformat(),
                "raw_text": message.text,
                "classification": classification,
                "execution": execution,
                "symbol": symbol,
                "has_media": message.has_media,
                "media_urls": list(message.media_urls),
                "external_urls": list(message.external_urls),
            },
        )
    )


def _record_audit_candidate(
    db: Session,
    message: TelegramChannelMessage,
    entry: EntryClassification,
    now: datetime,
) -> None:
    """Store broader entry language without allowing it to place an order."""

    market = _active_perpetual_market(db, entry.symbol)
    db.add(
        DecisionLog(
            event_type="the_bull_entry_candidate",
            market_id=market.id if market is not None else None,
            message=(
                f"The Bull entry candidate retained for audit: "
                f"{entry.symbol} ({entry.intent})"
            ),
            data={
                "message_id": message.message_id,
                "message_url": message.url,
                "published_at": message.published_at.isoformat(),
                "observed_at": now.isoformat(),
                "raw_text": message.text,
                "symbol": entry.symbol,
                "intent": entry.intent,
                "execution": "audit_only",
                "automatic_execution": False,
                "market_available": market is not None,
                "has_media": message.has_media,
                "media_urls": list(message.media_urls),
                "external_urls": list(message.external_urls),
            },
        )
    )


def _candidate_state(
    message: TelegramChannelMessage,
    entry: EntryClassification,
    now: datetime,
) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "message_url": message.url,
        "published_at": message.published_at.isoformat(),
        "observed_at": now.isoformat(),
        "raw_text": message.text,
        "symbol": entry.symbol,
        "intent": entry.intent,
        "execution": "audit_only",
    }


def _context_review_state(
    message: TelegramChannelMessage,
    symbol: str | None,
    now: datetime,
) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "message_url": message.url,
        "published_at": message.published_at.isoformat(),
        "observed_at": now.isoformat(),
        "raw_text": message.text,
        "symbol": symbol,
        "intent": "context_review",
        "execution": "manual_review",
        "has_media": message.has_media,
        "media_urls": list(message.media_urls),
        "external_urls": list(message.external_urls),
    }


def analyze_the_bull_chart(
    _settings: Settings,
    message: TelegramChannelMessage,
    _config: dict[str, Any],
) -> dict[str, Any]:
    """Read one public Telegram chart and return structured range evidence.

    The message and chart are untrusted external content.  This function never
    receives a database session and has no direct path to create a signal or
    order. OCR runs on this Mac through Apple Vision; no image is sent to a
    third party. The caller may route an in-range result through the ordinary
    source risk path.
    """
    if not message.media_urls:
        return {"status": "unavailable", "reason": "no supported public chart image"}

    try:
        image_bytes, content_type = _download_public_chart(message.media_urls[0])
        analysis = _infer_local_chart_zones(_run_macos_vision_ocr(image_bytes, content_type))
        marker = _red_axis_price_marker(image_bytes)
        if marker is not None:
            analysis["red_axis_marker"] = marker
            range_labels = _blue_axis_range_labels(image_bytes)
            for key, value in range_labels.items():
                analysis["chart_labels"][key] = bool(
                    analysis["chart_labels"].get(key) or value
                )
            try:
                axis_tokens = _ocr_near_red_axis_marker(image_bytes, marker)
                axis_labels = _chart_annotation_labels(axis_tokens)
                for key, value in axis_labels.items():
                    analysis["chart_labels"][key] = bool(
                        analysis["chart_labels"].get(key) or value
                    )
                interest_zone = _interest_zone_from_axis_tokens(marker, axis_tokens)
                if interest_zone is not None:
                    analysis["interest_zones"] = [interest_zone]
                    if not analysis["has_actionable_buy_zone"]:
                        analysis["summary"] = (
                            "Estimated the red right-axis interest range from nearby price ticks; "
                            "audit only, with no explicit buy-zone label."
                        )
                elif not analysis["has_actionable_buy_zone"]:
                    analysis["summary"] = (
                        "Found the red right-axis marker, but could not verify its numeric "
                        "price range; audit only."
                    )
            except Exception as exc:
                logger.warning(
                    "the_bull_axis_ocr_failed",
                    message_id=message.message_id,
                    error=_safe_error(exc),
                )
    except Exception as exc:
        logger.warning(
            "the_bull_chart_analysis_failed",
            message_id=message.message_id,
            error=_safe_error(exc),
        )
        return {"status": "failed", "reason": _safe_error(exc)}

    text_symbol = _ticker_symbol_from_message(message.text)
    if text_symbol is not None:
        analysis["symbol"] = text_symbol
    analysis["status"] = "reviewed"
    analysis["image_url"] = message.media_urls[0]
    return analysis


def _record_chart_analysis(
    db: Session,
    message: TelegramChannelMessage,
    analysis: dict[str, Any],
    now: datetime,
    execution: str = "audit_only",
) -> None:
    status = str(analysis.get("status") or "failed")
    has_buy_zone = bool(analysis.get("has_actionable_buy_zone"))
    symbol = analysis.get("symbol") if isinstance(analysis.get("symbol"), str) else None
    market = _active_perpetual_market(db, symbol)
    event_type = (
        "the_bull_chart_candidate"
        if status == "reviewed" and has_buy_zone
        else "the_bull_chart_reviewed"
        if status == "reviewed"
        else "the_bull_chart_analysis_failed"
    )
    db.add(
        DecisionLog(
            event_type=event_type,
            market_id=market.id if market is not None else None,
            message=(
                f"The Bull chart accumulation zone identified for {symbol or 'unknown symbol'}"
                if event_type == "the_bull_chart_candidate"
                else "The Bull chart reviewed"
                if event_type == "the_bull_chart_reviewed"
                else "The Bull chart could not be reviewed"
            ),
            data={
                "message_id": message.message_id,
                "message_url": message.url,
                "published_at": message.published_at.isoformat(),
                "reviewed_at": now.isoformat(),
                "image_url": analysis.get("image_url"),
                "symbol": symbol,
                "buy_zones": analysis.get("buy_zones", []),
                "interest_zones": analysis.get("interest_zones", []),
                "red_axis_marker": analysis.get("red_axis_marker"),
                "chart_labels": analysis.get("chart_labels", {}),
                "has_actionable_buy_zone": has_buy_zone,
                "confidence": analysis.get("confidence"),
                "summary": analysis.get("summary"),
                "reason": analysis.get("reason"),
                "execution": execution,
            },
        )
    )


def _chart_candidate_state(
    message: TelegramChannelMessage,
    analysis: dict[str, Any],
    now: datetime,
    execution: str = "audit_only",
) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "message_url": message.url,
        "published_at": message.published_at.isoformat(),
        "reviewed_at": now.isoformat(),
        "symbol": analysis.get("symbol"),
        "buy_zones": analysis.get("buy_zones", []),
        "interest_zones": analysis.get("interest_zones", []),
        "confidence": analysis.get("confidence"),
        "summary": analysis.get("summary"),
        "execution": execution,
    }


def _download_public_chart(url: str) -> tuple[bytes, str]:
    if not _is_supported_media_url(url):
        raise ValueError("chart image host is not allowed")
    with (
        httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False) as client,
        client.stream("GET", url, headers={"Accept": "image/*"}) as response,
    ):
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("chart response is not a supported image")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_CHART_IMAGE_BYTES:
                raise ValueError("chart image exceeds 8 MiB limit")
    if not content:
        raise ValueError("chart image is empty")
    return bytes(content), content_type


_LOCAL_VISION_SOURCE = Path(__file__).with_name("macos_chart_ocr.m")
_LOCAL_VISION_BINARY = Path(tempfile.gettempdir()) / "trado-macos-chart-ocr"
_LOCAL_VISION_TIMEOUT_SECONDS = 30
_PRICE_FRAGMENT_RE = re.compile(
    r"(?<![A-Za-z0-9])\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)([KMB])?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BUY_ZONE_LABEL_RE = re.compile(
    r"\b(?:buy(?:\s+zone)?|accumulation|support|dca|dollar\s+cost)\b",
    re.IGNORECASE,
)


def _run_macos_vision_ocr(image_bytes: bytes, content_type: str) -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        raise RuntimeError("local chart OCR requires macOS Vision")
    executable = _ensure_macos_vision_executable()
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type, ".image")
    image_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="trado-chart-", suffix=suffix, delete=False) as image_file:
            image_file.write(image_bytes)
            image_path = Path(image_file.name)
        completed = subprocess.run(
            [str(executable), str(image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_LOCAL_VISION_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError(_compact_process_error(completed.stderr, "local OCR failed"))
        result = json.loads(completed.stdout)
        if not isinstance(result, list):
            raise ValueError("local OCR returned an invalid payload")
        return [item for item in result if isinstance(item, dict)]
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)


def _ensure_macos_vision_executable() -> Path:
    if not _LOCAL_VISION_SOURCE.is_file():
        raise RuntimeError("local macOS Vision helper is unavailable")
    try:
        if (
            _LOCAL_VISION_BINARY.is_file()
            and _LOCAL_VISION_BINARY.stat().st_mtime >= _LOCAL_VISION_SOURCE.stat().st_mtime
        ):
            return _LOCAL_VISION_BINARY
    except OSError:
        pass

    compiled = _LOCAL_VISION_BINARY.with_name(f"{_LOCAL_VISION_BINARY.name}-{os.getpid()}")
    try:
        completed = subprocess.run(
            [
                "xcrun",
                "clang",
                "-fobjc-arc",
                "-framework",
                "Vision",
                "-framework",
                "AppKit",
                str(_LOCAL_VISION_SOURCE),
                "-o",
                str(compiled),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        compiled.unlink(missing_ok=True)
        raise RuntimeError("local OCR build could not run") from exc
    if completed.returncode != 0:
        compiled.unlink(missing_ok=True)
        raise RuntimeError(_compact_process_error(completed.stderr, "local OCR build failed"))
    os.replace(compiled, _LOCAL_VISION_BINARY)
    return _LOCAL_VISION_BINARY


def _infer_local_chart_zones(tokens: list[dict[str, Any]]) -> dict[str, Any]:
    chart_labels = _chart_annotation_labels(tokens)
    labels = [token for token in tokens if _BUY_ZONE_LABEL_RE.search(_ocr_text(token))]
    if not labels:
        return {
            "symbol": None,
            "buy_zones": [],
            "interest_zones": [],
            "chart_labels": chart_labels,
            "has_actionable_buy_zone": False,
            "confidence": "low",
            "summary": "Local Vision OCR found no buy, accumulation, or support label.",
        }

    axis = _price_axis_from_ocr(tokens)
    zones: list[dict[str, str]] = []
    confidence = "low"
    for label_token in labels:
        direct_prices = _prices_from_ocr_text(_ocr_text(label_token))
        if len(direct_prices) >= 2:
            lower, upper = min(direct_prices), max(direct_prices)
            zone_confidence = "high"
        elif axis is not None:
            center = axis["slope"] * _ocr_center_y(label_token) + axis["intercept"]
            half_width = axis["typical_step"] * Decimal("0.30")
            lower = center - half_width
            upper = center + half_width
            if lower <= 0:
                continue
            zone_confidence = "medium"
        else:
            continue

        zone = {
            "lower": _format_chart_price(lower),
            "upper": _format_chart_price(upper),
            "label": _zone_label(_ocr_text(label_token)),
        }
        if zone not in zones:
            zones.append(zone)
        if zone_confidence == "high":
            confidence = "high"
        elif confidence != "high":
            confidence = "medium"
        if len(zones) == 3:
            break

    summary = (
        "Local macOS Vision OCR read a price range printed with the chart label."
        if confidence == "high"
        else "Local macOS Vision OCR estimated a range from the chart price scale."
        if confidence == "medium"
        else "Local Vision OCR found a zone label but no reliable numerical price range."
    )
    return {
        "symbol": None,
        "buy_zones": zones,
        "interest_zones": [],
        "chart_labels": chart_labels,
        "has_actionable_buy_zone": bool(zones) and confidence in {"medium", "high"},
        "confidence": confidence,
        "summary": summary,
    }


def _chart_annotation_labels(tokens: list[dict[str, Any]]) -> dict[str, bool]:
    """Chart labels are context, never an entry instruction on their own."""
    texts = [_ocr_text(token) for token in tokens]
    target = any(re.search(r"\btarget\b", text, re.IGNORECASE) for text in texts)
    stop = any(re.search(r"\bstop\b", text, re.IGNORECASE) for text in texts)
    return {
        "low": any(re.search(r"\b(?:low|lon)\b", text, re.IGNORECASE) for text in texts),
        "high": any(
            re.search(r"\bhigh\b|^h[ao0]\b\s*[:.]?", text, re.IGNORECASE)
            for text in texts
        ),
        "long_position": target and stop
        or any(re.search(r"\blong\b", text, re.IGNORECASE) for text in texts),
        "target": target,
        "stop": stop,
    }


def _red_axis_price_marker(image_bytes: bytes) -> dict[str, int] | None:
    """Locate TradingView's small red current-price tag on the right axis."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width < 300 or height < 180:
        return None
    x_start = int(width * 0.90)
    rows: list[tuple[int, list[int]]] = []
    pixels = image.load()
    assert pixels is not None
    for y in range(int(height * 0.08), int(height * 0.85)):
        xs: list[int] = []
        for x in range(x_start, width):
            red, green, blue = cast(tuple[int, int, int], pixels[x, y])
            if red > 120 and green < 130 and red > green * 1.5 and red > blue * 1.4:
                xs.append(x)
        if len(xs) >= max(3, width // 250):
            rows.append((y, xs))
    runs: list[list[tuple[int, list[int]]]] = []
    for row in rows:
        if not runs or row[0] > runs[-1][-1][0] + 1:
            runs.append([])
        runs[-1].append(row)
    candidates: list[tuple[int, dict[str, int]]] = []
    for run in runs:
        xs = [x for _, row_xs in run for x in row_xs]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = run[0][0], run[-1][0]
        if not (3 <= y_max - y_min + 1 <= height * 0.06):
            continue
        if not (width * 0.92 <= x_min and 6 <= x_max - x_min + 1 <= width * 0.08):
            continue
        candidates.append(
            (
                len(xs),
                {
                    "x_min": x_min,
                    "x_max": x_max,
                    "y_min": y_min,
                    "y_max": y_max,
                    "y_center": (y_min + y_max) // 2,
                    "image_width": width,
                    "image_height": height,
                },
            )
        )
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _blue_axis_range_labels(image_bytes: bytes) -> dict[str, bool]:
    """Recognize the two small gray-blue High/Low tags by their axis layout."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    width, height = image.size
    pixels = image.load()
    assert pixels is not None
    rows: list[tuple[int, int]] = []
    for y in range(int(height * 0.05), int(height * 0.85)):
        count = 0
        for x in range(int(width * 0.925), int(width * 0.98)):
            red, green, blue = cast(tuple[int, int, int], pixels[x, y])
            if blue > 100 and blue > red * 1.14 and blue > green * 1.02:
                count += 1
        if count >= 2:
            rows.append((y, count))
    runs: list[list[tuple[int, int]]] = []
    for row in rows:
        if not runs or row[0] > runs[-1][-1][0] + 1:
            runs.append([])
        runs[-1].append(row)
    centers = [
        (run[0][0] + run[-1][0]) // 2
        for run in runs
        if 2 <= len(run) <= 10 and sum(count for _, count in run) >= 8
    ]
    paired = len(centers) >= 2 and max(centers) - min(centers) >= height * 0.2
    return {"high": paired, "low": paired}


def _ocr_near_red_axis_marker(
    image_bytes: bytes, marker: dict[str, int]
) -> list[dict[str, Any]]:
    """Upscale the tiny price scale and map Vision boxes to original pixels."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    width, height = image.size
    if width * height > 3_000_000:
        raise ValueError("chart is too large for bounded axis OCR")
    center = marker["y_center"]
    tokens: list[dict[str, Any]] = []
    for left_fraction, vertical_pad, scale in ((0.875, 35, 6), (0.925, 25, 8), (0.75, height, 5)):
        left = int(width * left_fraction)
        top = max(0, center - vertical_pad)
        bottom = min(height, center + vertical_pad)
        if left >= width or top >= bottom:
            continue
        crop = image.crop((left, top, width, bottom))
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        output = BytesIO()
        crop.save(output, format="PNG")
        for token in _run_macos_vision_ocr(output.getvalue(), "image/png"):
            mapped = dict(token)
            mapped["x_pixel"] = left + _ocr_decimal(token.get("x")) * (width - left)
            mapped["y_pixel"] = bottom - _ocr_center_y(token) * (bottom - top)
            tokens.append(mapped)
    return tokens


def _axis_price_candidates(text: str) -> list[Decimal]:
    cleaned = re.sub(r"\s+", "", text).replace("$", "")
    if match := re.fullmatch(r"[0189]/(\d{4,6})", cleaned):
        return [Decimal("0." + match.group(1))]
    if match := re.fullmatch(r"(\d{1,3})[.,](\d{2,6})", cleaned):
        whole, fraction = match.groups()
        if len(whole) >= 2 and len(fraction) == 3 and "," in cleaned:
            return [Decimal(whole + fraction)]
        return [Decimal(f"{whole}.{fraction}")]
    return []


def _interest_zone_from_axis_tokens(
    marker: dict[str, int], tokens: list[dict[str, Any]]
) -> dict[str, str | bool] | None:
    """Interpolate the red tag between nearby axis ticks; never make it auto-executable."""
    center_y = Decimal(marker["y_center"])
    points: list[tuple[Decimal, Decimal]] = []
    for token in tokens:
        y = _ocr_decimal(token.get("y_pixel"))
        x = _ocr_decimal(token.get("x_pixel"))
        if x < Decimal(marker["image_width"]) * Decimal("0.94"):
            continue
        if abs(y - center_y) > 55:
            continue
        points.extend((price, y) for price in _axis_price_candidates(_ocr_text(token)))
    # Vision can split an axis tick such as "1.300" into "1" and "300".
    for first in tokens:
        for second in tokens:
            if first is second:
                continue
            if not re.fullmatch(r"\d", _ocr_text(first)) or not re.fullmatch(
                r"\d{3,5}", _ocr_text(second)
            ):
                continue
            first_y = _ocr_decimal(first.get("y_pixel"))
            second_y = _ocr_decimal(second.get("y_pixel"))
            first_x = _ocr_decimal(first.get("x_pixel"))
            second_x = _ocr_decimal(second.get("x_pixel"))
            if abs(first_y - second_y) <= 2 and 0 < second_x - first_x <= 12:
                points.append((Decimal(f"{_ocr_text(first)}.{_ocr_text(second)}"), first_y))
    pairs: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for upper_price, upper_y in points:
        if not 5 <= center_y - upper_y <= 45:
            continue
        for lower_price, lower_y in points:
            if not 5 <= lower_y - center_y <= 45 or upper_price <= lower_price:
                continue
            relative_step = (upper_price - lower_price) / lower_price
            if not Decimal("0.005") <= relative_step <= Decimal("0.5"):
                continue
            pairs.append((upper_price, upper_y, lower_price, lower_y))
    if not pairs:
        return None
    upper_price, upper_y, lower_price, lower_y = min(
        pairs, key=lambda pair: abs(center_y - pair[1]) + abs(pair[3] - center_y)
    )
    estimate = upper_price + (lower_price - upper_price) * (
        (center_y - upper_y) / (lower_y - upper_y)
    )
    tolerance = max(estimate * Decimal("0.03"), (upper_price - lower_price) * Decimal("0.15"))
    tolerance = min(tolerance, estimate * Decimal("0.08"))
    return {
        "lower": _format_chart_price(estimate - tolerance),
        "upper": _format_chart_price(estimate + tolerance),
        "center": _format_chart_price(estimate),
        "label": "red_axis_interest",
        "source": "nearby_axis_ticks_interpolated",
        "automatic_execution": False,
    }


def _price_axis_from_ocr(tokens: list[dict[str, Any]]) -> dict[str, Decimal] | None:
    points: list[tuple[Decimal, Decimal]] = []
    for token in tokens:
        if _ocr_x(token) < Decimal("0.72"):
            continue
        prices = _prices_from_ocr_text(_ocr_text(token))
        if len(prices) == 1:
            points.append((prices[0], _ocr_center_y(token)))
    if len(points) < 3:
        return None
    points.sort(key=lambda point: point[1])
    y_mean = sum((point[1] for point in points), Decimal("0")) / len(points)
    price_mean = sum((point[0] for point in points), Decimal("0")) / len(points)
    numerator = sum(
        ((y - y_mean) * (price - price_mean) for price, y in points), Decimal("0")
    )
    denominator = sum(((y - y_mean) ** 2 for _, y in points), Decimal("0"))
    if denominator <= 0:
        return None
    slope = numerator / denominator
    if slope <= 0:
        return None
    intercept = price_mean - slope * y_mean
    steps = [abs(points[index][0] - points[index - 1][0]) for index in range(1, len(points))]
    positive_steps = sorted(step for step in steps if step > 0)
    if not positive_steps:
        return None
    return {
        "slope": slope,
        "intercept": intercept,
        "typical_step": positive_steps[len(positive_steps) // 2],
    }


def _prices_from_ocr_text(text: str) -> list[Decimal]:
    prices: list[Decimal] = []
    for raw_number, suffix in _PRICE_FRAGMENT_RE.findall(text):
        try:
            value = Decimal(raw_number.replace(",", ""))
        except Exception:
            continue
        multiplier = {"K": Decimal("1000"), "M": Decimal("1000000"), "B": Decimal("1000000000")}.get(
            suffix.upper(), Decimal("1")
        )
        value *= multiplier
        if value.is_finite() and value > 0:
            prices.append(value)
    return prices


def _ocr_text(token: dict[str, Any]) -> str:
    text = token.get("text")
    return _normalize_message_text(text) if isinstance(text, str) else ""


def _ocr_x(token: dict[str, Any]) -> Decimal:
    return _ocr_decimal(token.get("x"))


def _ocr_center_y(token: dict[str, Any]) -> Decimal:
    return _ocr_decimal(token.get("y")) + _ocr_decimal(token.get("height")) / Decimal("2")


def _ocr_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


def _format_chart_price(value: Decimal) -> str:
    """Keep local OCR estimates readable without discarding tiny-coin precision."""
    if value <= 0:
        return "0"
    try:
        rounded = value.quantize(Decimal("1").scaleb(value.adjusted() - 5))
    except Exception:
        rounded = value
    rendered = format(rounded, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _zone_label(text: str) -> str:
    normalized = _normalize_message_text(text)
    return normalized[:120] if normalized else "accumulation zone"


def _compact_process_error(value: str, fallback: str) -> str:
    compact = _normalize_message_text(value)
    return compact[:500] if compact else fallback


def _active_perpetual_market(db: Session, symbol: str | None) -> Market | None:
    if not symbol:
        return None
    return (
        db.query(Market)
        .filter(
            Market.exchange == "hyperliquid",
            Market.base == symbol,
            Market.market_type == MarketType.PERP.value,
            Market.active.is_(True),
        )
        .first()
    )


def _active_the_bull_market(
    db: Session,
    symbol: str | None,
    *,
    allow_spot_fallback: bool,
) -> Market | None:
    """Prefer a perpetual and use only a USDC spot pair when explicitly enabled."""
    perpetual = _active_perpetual_market(db, symbol)
    if perpetual is not None or not allow_spot_fallback or not symbol:
        return perpetual
    return (
        db.query(Market)
        .filter(
            Market.exchange == "hyperliquid",
            Market.base == symbol,
            Market.market_type == MarketType.SPOT.value,
            Market.quote == "USDC",
            Market.active.is_(True),
        )
        .first()
    )


def _ticker_symbol_from_message(text: str) -> str | None:
    for match in _TICKER_RE.finditer(_normalize_message_text(text)):
        symbol = _normalize_symbol(match.group(1))
        if _is_ticker_symbol(symbol):
            return symbol
    return None


def _is_ticker_symbol(symbol: str) -> bool:
    """Bare dollar amounts such as $189 or $189M are not tradeable tickers."""
    return any(character.isalpha() for character in symbol) and not re.fullmatch(
        r"[0-9]+[KMBT]", symbol
    )


def _hashtag_chart_entry_symbol(message: TelegramChannelMessage) -> str | None:
    """Treat a hashtag-only chart post as an explicit source call when enabled.

    The Bull uses this compact format for some entries. It is deliberately
    restricted to a single hashtag and an attached chart so generic hashtags
    or commentary cannot become trade instructions.
    """
    if not message.has_media:
        return None
    match = _HASHTAG_ONLY_TICKER_RE.fullmatch(_normalize_message_text(message.text))
    if match is None:
        return None
    symbol = _normalize_symbol(match.group(1))
    return symbol if _is_ticker_symbol(symbol) else None


def _chart_ocr_fallback_symbol(
    message: TelegramChannelMessage,
    chart_result: dict[str, Any] | None,
    entry: EntryClassification | None,
    config: dict[str, Any],
) -> str | None:
    """Return the caption ticker only when OCR supplied no actionable range.

    A successfully read buy range remains authoritative: if the midpoint falls
    outside it, the source call is skipped rather than silently opened at the
    current price. The fallback is only for a chart with no usable OCR result,
    a ticker-only Telegram caption, and the explicit runtime opt-in. A ticker
    embedded in commentary or news is not an instruction to buy at market.
    Averaging/add messages are deliberately never promoted this way.
    """
    if not (
        message.has_media
        and bool(config["chart_analysis_enabled"])
        and bool(config["chart_zone_execution"])
        and bool(config["chart_ocr_fallback_execution"])
    ):
        return None
    if entry is not None and entry.intent == "averaging_add":
        return None
    if chart_result is None:
        return None
    if (
        str(chart_result.get("status") or "") == "reviewed"
        and bool(chart_result.get("has_actionable_buy_zone"))
    ):
        return None
    match = _TICKER_ONLY_CAPTION_RE.fullmatch(_normalize_message_text(message.text))
    if match is None:
        return None
    symbol = _normalize_symbol(match.group(1))
    return symbol if _is_ticker_symbol(symbol) else None


def _requires_context_review(message: TelegramChannelMessage) -> bool:
    return bool(
        _ticker_symbol_from_message(message.text)
        and (message.has_media or message.external_urls)
    )


def _can_execute_expanded_entry(
    entry: EntryClassification | None,
    config: dict[str, Any],
) -> bool:
    """Route the user-enabled The Bull call vocabulary through normal guards."""
    return bool(
        entry is not None
        and bool(config["expanded_entry_execution"])
        and entry.intent in _EXPANDED_EXECUTABLE_INTENTS
        and (entry.intent != "averaging_add" or config["execution_mode"] == "live")
    )


def _handle_chart_zone_entry(
    db: Session,
    settings: Settings,
    executor: OrderExecutionEngine,
    message: TelegramChannelMessage,
    chart_result: dict[str, Any],
    mids: dict[str, str],
    config: dict[str, Any],
    now: datetime,
) -> tuple[Signal | None, int]:
    """Route a verified chart range through the ordinary source risk path.

    OCR evidence is never enough on its own: the message must identify a
    listed coin and the live midpoint must be inside one of the extracted
    ranges before the regular stale-message, position-cap, risk-plan and
    execution-mode checks are reached.
    """
    symbol = chart_result.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return None, 0
    zones = chart_result.get("buy_zones")
    if not isinstance(zones, list) or not zones:
        return _skip_entry(db, message, symbol, "chart contains no usable price range")

    market = _active_the_bull_market(
        db,
        symbol,
        allow_spot_fallback=bool(config["spot_fallback_enabled"]),
    )
    if market is None:
        return _skip_entry(
            db,
            message,
            symbol,
            "chart symbol has no active Hyperliquid perpetual or eligible USDC spot market",
        )
    raw_mid = mids.get(coin_for_market(market)) or mids.get(market.symbol)
    try:
        mid = Decimal(str(raw_mid))
    except Exception:
        mid = Decimal("0")
    if not mid.is_finite() or mid <= 0:
        return _skip_entry(db, message, symbol, "current Hyperliquid midpoint is unavailable")

    if not any(_chart_zone_contains_mid(zone, mid) for zone in zones):
        return _skip_entry(db, message, symbol, "current midpoint is outside the chart buy zone")

    return _handle_entry_message(
        db,
        settings,
        executor,
        message,
        symbol,
        mids,
        config,
        now,
        "chart_zone",
    )


def _chart_zone_contains_mid(zone: object, mid: Decimal) -> bool:
    if not isinstance(zone, dict):
        return False
    try:
        lower = Decimal(str(zone.get("lower")))
        upper = Decimal(str(zone.get("upper")))
    except Exception:
        return False
    if not lower.is_finite() or not upper.is_finite() or lower <= 0 or upper < lower:
        return False
    return lower <= mid <= upper


def _handle_entry_message(
    db: Session,
    settings: Settings,
    executor: OrderExecutionEngine,
    message: TelegramChannelMessage,
    symbol: str,
    mids: dict[str, str],
    config: dict[str, Any],
    now: datetime,
    entry_intent: str,
) -> tuple[Signal | None, int]:
    existing = _find_external_signal(db, message.message_id)
    if existing is not None:
        return existing, 0

    if message.published_at > now + timedelta(minutes=2):
        return _skip_entry(db, message, symbol, "message timestamp is in the future")
    age = now - message.published_at
    if age > timedelta(minutes=int(config["max_signal_age_minutes"])):
        return _skip_entry(
            db,
            message,
            symbol,
            f"message is stale ({age.total_seconds() / 60:.1f} minutes old)",
        )

    market = _active_the_bull_market(
        db,
        symbol,
        allow_spot_fallback=bool(config["spot_fallback_enabled"]),
    )
    if market is None:
        return _skip_entry(
            db,
            message,
            symbol,
            "no active Hyperliquid perpetual or eligible USDC spot market",
        )

    source_mode = (
        OrderMode.LIVE.value
        if str(config["execution_mode"]) == "live"
        else OrderMode.PAPER.value
    )
    # A pre-existing paper companion is research evidence, not exposure on
    # Hyperliquid.  It must never veto a later live The Bull call for the same
    # market; only an already open live position is a duplicate in live mode.
    requested_modes = (
        [OrderMode.LIVE.value]
        if str(config["execution_mode"]) == "live"
        else [OrderMode.PAPER.value]
    )
    existing_position = (
        db.query(Position)
        .filter(
            Position.market_id == market.id,
            Position.status == "open",
            Position.mode.in_(requested_modes),
        )
        .first()
    )
    if existing_position is not None:
        if not (
            source_mode == OrderMode.LIVE.value
            and entry_intent in {"buy_the_dip", "averaging_add"}
            and _position_is_from_the_bull(existing_position)
        ):
            return _skip_entry(
                db,
                message,
                symbol,
                f"an open {existing_position.mode} position already exists; addition is not eligible",
            )
    elif entry_intent == "averaging_add":
        return _skip_entry(db, message, symbol, "adding more requires an existing The Bull position")
    else:
        source_open_positions = [
            position
            for position in db.query(Position)
            .filter(Position.status == "open", Position.mode == source_mode)
            .all()
            if _position_is_from_the_bull(position)
        ]
        if len(source_open_positions) >= int(config["max_open_positions"]):
            return _skip_entry(
                db,
                message,
                symbol,
                "source-specific maximum open positions reached "
                f"({len(source_open_positions)}/{config['max_open_positions']})",
            )

    coin = coin_for_market(market)
    raw_mid = mids.get(coin) or mids.get(market.symbol)
    try:
        mid = Decimal(str(raw_mid))
    except Exception:
        mid = Decimal("0")
    if not mid.is_finite() or mid <= 0:
        return _skip_entry(db, message, symbol, "current Hyperliquid midpoint is unavailable")

    timeframe = str(config["timeframe"])
    rows = (
        db.query(Candle)
        .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(120)
        .all()
    )
    candles = [
        CandleInput(
            open_time=row.open_time,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            open_interest=row.open_interest,
        )
        for row in reversed(rows)
    ]
    candles.append(
        CandleInput(
            open_time=now,
            open=float(mid),
            high=float(mid),
            low=float(mid),
            close=float(mid),
            volume=0.0,
        )
    )
    risk_value = setting_value(db, "risk")
    try:
        min_risk_reward = max(2.0, float(risk_value.get("min_risk_reward", 2.0)))
    except (TypeError, ValueError):
        min_risk_reward = 2.0
    plan = RiskManager().build_plan(
        candles,
        Direction.LONG,
        signal_score=int(config["signal_score"]),
        min_risk_reward=min_risk_reward,
        min_stop_distance_pct=float(config["min_stop_distance_pct"]),
    )
    if plan is None:
        return _skip_entry(
            db,
            message,
            symbol,
            f"risk plan unavailable from {timeframe} market structure",
        )

    signal = Signal(
        market_id=market.id,
        timeframe=timeframe,
        direction=Direction.LONG.value,
        score=int(config["signal_score"]),
        confidence_label="external",
        entry_price=Decimal(str(plan.entry)),
        stop_loss=Decimal(str(plan.stop_loss)),
        take_profit_1=Decimal(str(plan.take_profit_1)),
        take_profit_2=Decimal(str(plan.take_profit_2)),
        take_profit_3=(Decimal(str(plan.take_profit_3)) if plan.take_profit_3 else None),
        risk_reward=Decimal(str(plan.risk_reward)),
        reason=f"The Bull public signal: {message.text}",
        strategy_breakdown={
            "_external_source": {
                "provider": "the_bull",
                "channel_username": message.channel_username,
                "message_id": message.message_id,
                "message_url": message.url,
                "published_at": message.published_at.isoformat(),
                "raw_text": message.text,
                "risk_multiplier": float(config["risk_multiplier"]),
                "entry_intent": entry_intent,
                "is_addition": existing_position is not None,
                "instrument_type": market.market_type,
                "spot_fallback": market.market_type == MarketType.SPOT.value,
            }
        },
        status=SignalStatus.NEW.value,
        source_candle_open_time=None,
    )
    db.add(signal)
    db.flush()
    db.add(
        DecisionLog(
            event_type="the_bull_signal_created",
            market_id=market.id,
            signal_id=signal.id,
            message=f"External LONG signal created for {symbol}",
            data={
                "message_id": message.message_id,
                "message_url": message.url,
                "entry_price": str(signal.entry_price),
                "stop_loss": str(signal.stop_loss),
                "timeframe": timeframe,
                "risk_multiplier": config["risk_multiplier"],
                "instrument_type": market.market_type,
            },
        )
    )
    db.commit()

    operation_mode = effective_operation_mode(db, settings)
    execution_mode = str(config["execution_mode"])
    if execution_mode == "paper" and operation_mode not in {
        OperationMode.PAPER_TRADING,
        OperationMode.AUTO_TRADE,
    }:
        _record_execution_blocked(db, signal, execution_mode, operation_mode)
        return signal, 0
    if execution_mode == "live" and operation_mode != OperationMode.AUTO_TRADE:
        _record_execution_blocked(db, signal, execution_mode, operation_mode)
        return signal, 0

    if execution_mode == "paper":
        paper_position = executor.execute_paper(db, signal)
        return signal, 1 if paper_position is not None else 0
    if existing_position is not None:
        try:
            executor.execute_live_addition(
                db,
                signal,
                existing_position,
                max_additions=int(config["max_additions_per_ticker"]),
                max_fraction=Decimal(str(config["max_addition_fraction"])),
            )
        except PermissionError as exc:
            # Preflight rejections are expected (for example an authorized 30%
            # add below the exchange minimum). Once a market order was submitted,
            # any failure must remain visible as a processing error for review.
            if db.query(Order).filter(Order.signal_id == signal.id).first() is not None:
                raise
            signal.status = SignalStatus.IGNORED.value
            db.add(signal)
            db.add(DecisionLog(
                event_type="the_bull_addition_skipped",
                market_id=market.id,
                signal_id=signal.id,
                position_id=existing_position.id,
                message="The Bull addition did not pass live preflight",
                data={"reason": str(exc), "message_id": message.message_id},
            ))
            db.commit()
        return signal, 0
    # The paper companion is useful for forward evidence, but simulated
    # equity/minimum-notional constraints must not veto a live The Bull entry
    # that has independently passed every live guardrail and exchange preflight.
    executor.execute_paper(db, signal)
    executor.execute_live(db, signal)
    return signal, 1


def _handle_exit_message(
    db: Session,
    executor: OrderExecutionEngine,
    message: TelegramChannelMessage,
    reason: str,
    tracked: dict[str, Any],
    mids: dict[str, str],
    config: dict[str, Any],
) -> int:
    parent_id = message.reply_to_message_id
    if parent_id is None:
        return 0
    tracked_entry = tracked.get(str(parent_id))
    signal_id = _safe_int(tracked_entry.get("signal_id")) if isinstance(tracked_entry, dict) else None
    signal = db.get(Signal, signal_id) if signal_id is not None else _find_external_signal(db, parent_id)
    if signal is None:
        return 0

    positions = (
        db.query(Position)
        .filter(Position.market_id == signal.market_id, Position.status == "open")
        .all()
    )
    matching = [
        position
        for position in positions
        if _safe_int((position.metadata_json or {}).get("source_signal_id")) == signal.id
    ]
    if not bool(config["close_companion_paper_on_source_exit"]):
        matching = [position for position in matching if position.mode == OrderMode.LIVE.value]

    closed = 0
    for position in sorted(matching, key=lambda row: row.mode == OrderMode.PAPER.value):
        if position.mode == OrderMode.LIVE.value:
            executor.close_live_position(db, position, reason)
            closed += 1
            continue
        market = db.get(Market, position.market_id)
        if market is None:
            continue
        raw_mid = mids.get(coin_for_market(market)) or mids.get(market.symbol)
        try:
            exit_price = Decimal(str(raw_mid))
        except Exception:
            exit_price = Decimal("0")
        if not exit_price.is_finite() or exit_price <= 0:
            continue
        executor.close_position(db, position, exit_price, reason)
        closed += 1

    db.add(
        DecisionLog(
            event_type="the_bull_exit_processed",
            market_id=signal.market_id,
            signal_id=signal.id,
            message="The Bull source exit update processed",
            data={
                "message_id": message.message_id,
                "reply_to_message_id": parent_id,
                "reason": reason,
                "positions_closed": closed,
                "raw_text": message.text,
            },
        )
    )
    db.commit()
    return closed


def _find_external_signal(db: Session, source_message_id: int) -> Signal | None:
    rows = db.query(Signal).order_by(Signal.created_at.desc()).limit(500).all()
    for signal in rows:
        breakdown = signal.strategy_breakdown or {}
        source = breakdown.get("_external_source") if isinstance(breakdown, dict) else None
        if not isinstance(source, dict) or source.get("provider") != "the_bull":
            continue
        if _safe_int(source.get("message_id")) == source_message_id:
            return signal
    return None


def _position_is_from_the_bull(position: Position) -> bool:
    metadata = position.metadata_json or {}
    source = metadata.get("external_source") if isinstance(metadata, dict) else None
    return isinstance(source, dict) and source.get("provider") == "the_bull"


def _skip_entry(
    db: Session,
    message: TelegramChannelMessage,
    symbol: str,
    reason: str,
) -> tuple[None, int]:
    db.add(
        DecisionLog(
            event_type="the_bull_signal_skipped",
            message=f"The Bull signal skipped for {symbol}: {reason}",
            data={
                "symbol": symbol,
                "reason": reason,
                "message_id": message.message_id,
                "message_url": message.url,
                "raw_text": message.text,
            },
        )
    )
    db.commit()
    return None, 0


def _record_execution_blocked(
    db: Session,
    signal: Signal,
    execution_mode: str,
    operation_mode: OperationMode,
) -> None:
    db.add(
        DecisionLog(
            event_type="the_bull_execution_blocked",
            market_id=signal.market_id,
            signal_id=signal.id,
            message="The Bull automatic execution blocked by global operation mode",
            data={
                "source_execution_mode": execution_mode,
                "operation_mode": operation_mode.value,
            },
        )
    )
    db.commit()


def _record_poll_error(db: Session, config: dict[str, Any], exc: Exception) -> None:
    now = datetime.now(UTC)
    state_row = db.get(AppSetting, THE_BULL_STATE_KEY)
    state = dict(state_row.value) if state_row is not None and isinstance(state_row.value, dict) else {}
    state.update({"last_polled_at": now.isoformat(), "last_error": _safe_error(exc)})
    _write_state(db, state_row, state)
    db.add(
        DecisionLog(
            event_type="the_bull_poll_failed",
            message="The Bull public feed poll failed",
            data={
                "channel_username": config["channel_username"],
                "error": _safe_error(exc),
            },
        )
    )
    db.commit()


def _write_state(
    db: Session,
    row: AppSetting | None,
    state: dict[str, Any],
) -> AppSetting:
    if row is None:
        row = AppSetting(key=THE_BULL_STATE_KEY, value=dict(state))
        db.add(row)
    else:
        row.value = dict(state)
    return row


def _trim_tracked_messages(tracked: dict[str, Any]) -> dict[str, Any]:
    ordered_keys = sorted(tracked, key=lambda value: _safe_int(value) or 0)
    keep = ordered_keys[-_MAX_TRACKED_MESSAGES:]
    return {key: tracked[key] for key in keep}


def _message_id_from_url(value: str) -> int | None:
    path = urlparse(value).path.rstrip("/")
    if not path:
        return None
    return _safe_int(path.rsplit("/", 1)[-1])


def _is_supported_media_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme == "https"
        and host
        and port is None
        and not parsed.username
        and not parsed.password
        and _ALLOWED_MEDIA_HOST_RE.fullmatch(host)
        and parsed.path
    )


def _normalize_message_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _normalize_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Z0-9]", "", value.upper())
    for suffix in _QUOTE_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix) + 1:
            return symbol[: -len(suffix)]
    return symbol


def _safe_channel_username(value: object) -> str:
    channel = str(value or DEFAULT_THE_BULL_SETTINGS["channel_username"]).strip()
    channel = channel.removeprefix("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", channel):
        return str(DEFAULT_THE_BULL_SETTINGS["channel_username"])
    return channel


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    parsed = _safe_int(value)
    if parsed is None:
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_error(exc: Exception, max_length: int = 500) -> str:
    message = str(exc)
    return message if len(message) <= max_length else f"{message[:max_length]}..."
