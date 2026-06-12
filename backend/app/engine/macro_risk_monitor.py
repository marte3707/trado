from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html import unescape
import re
from statistics import median
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.runtime_settings import (
    DEFAULT_MACRO_RISK_STATE,
    effective_macro_risk_monitor_settings,
)
from app.db.models import AppSetting, Candle, DecisionLog, Market

LEVEL_ORDER = {"off": 0, "watch": 1, "high": 2, "extreme": 3}
MARKET_TARGET_BASES = {"BTC", "ETH"}
NEWS_FETCH_TIMEOUT_SECONDS = 6
CALENDAR_FETCH_TIMEOUT_SECONDS = 6
CALENDAR_DEFAULT_TZ = ZoneInfo("America/New_York")
CALENDAR_FETCH_HEADERS = {
    "User-Agent": "TradoMacroRiskMonitor/1.0 (+https://trado.local)",
    "Accept": "text/calendar,text/plain,*/*",
}


@dataclass(frozen=True)
class MacroNewsItem:
    title: str
    source: str
    url: str | None
    published_at: datetime
    summary: str | None = None


class MacroNewsClient:
    def fetch(self, urls: list[str]) -> list[MacroNewsItem]:
        items: list[MacroNewsItem] = []
        with httpx.Client(timeout=NEWS_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            for url in urls:
                response = client.get(url)
                response.raise_for_status()
                items.extend(parse_feed(response.text, source_url=url))
        return items


@dataclass(frozen=True)
class MacroCalendarEvent:
    title: str
    source: str
    starts_at: datetime
    category: str = "macro_calendar"
    score: int | None = None
    url: str | None = None
    key: str | None = None


class MacroCalendarClient:
    def fetch(self, urls: list[str]) -> list[MacroCalendarEvent]:
        events: list[MacroCalendarEvent] = []
        with httpx.Client(
            timeout=CALENDAR_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=CALENDAR_FETCH_HEADERS,
        ) as client:
            for url in urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                events.extend(parse_ics_calendar(response.text, source_url=url))
        return events


class MacroRiskMonitor:
    """Automatic macro-risk state machine.

    The monitor combines headline risk with local BTC/ETH market stress. It
    writes an operational state consumed by PortfolioGuard; it does not place or
    close orders directly.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        news_client: MacroNewsClient | None = None,
        calendar_client: MacroCalendarClient | None = None,
    ) -> None:
        self.settings = settings
        self.news_client = news_client or MacroNewsClient()
        self.calendar_client = calendar_client or MacroCalendarClient()

    def run(self, db: Session, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        config = effective_macro_risk_monitor_settings(db)
        state_row = db.get(AppSetting, "macro_risk_state")
        previous = _merged_state(state_row.value if state_row is not None else {})

        if not bool(config.get("enabled", True)):
            state = _merged_state(
                {
                    "active": False,
                    "level": "off",
                    "reason": "monitor_disabled",
                    "active_until": None,
                    "last_checked_at": now.isoformat(),
                    "policy": {},
                }
            )
            changed = _state_changed(previous, state)
            _upsert_state(db, state_row, state)
            if changed:
                _log_transition(db, previous, state, status="disabled")
            return {
                "status": "disabled",
                "changed": changed,
                "state": state,
                "previous_state": previous,
            }

        news_result = self._news_score(db, config, previous, now)
        calendar_result = self._calendar_score(db, config, previous, now)
        market_result = self._market_stress_score(db, config, now)
        combined_score = _combined_score(
            news_result["score"],
            calendar_result["score"],
            market_result["score"],
        )
        raw_level = _level_for_score(combined_score, config)
        state = self._next_state(
            config=config,
            previous=previous,
            now=now,
            raw_level=raw_level,
            combined_score=combined_score,
            news_result=news_result,
            calendar_result=calendar_result,
            market_result=market_result,
        )
        changed = _state_changed(previous, state)
        _upsert_state(db, state_row, state)
        if changed:
            _log_transition(db, previous, state, status="updated")
        return {
            "status": "updated",
            "changed": changed,
            "state": state,
            "previous_state": previous,
        }

    def _news_score(
        self,
        db: Session,
        config: dict[str, Any],
        previous: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if not bool(config.get("news_enabled", True)):
            return {"score": 0, "items": [], "errors": [], "fetched": False}

        cooldown_minutes = max(1, int(_float(config.get("news_fetch_cooldown_minutes"), 15)))
        last_fetch = _parse_dt(previous.get("last_news_fetch_at"))
        cached_items = _deserialize_news_items(previous.get("cached_news_items"), now)
        should_fetch = last_fetch is None or now - last_fetch >= timedelta(minutes=cooldown_minutes)
        items = cached_items
        fetched = False
        errors: list[str] = []

        if should_fetch:
            urls = [str(url).strip() for url in config.get("news_feed_urls") or [] if str(url).strip()]
            try:
                items = self.news_client.fetch(urls) if urls else []
                fetched = True
            except Exception as exc:
                errors.append(str(exc)[:240])
                db.add(
                    DecisionLog(
                        event_type="macro_risk_news_fetch_failed",
                        message="Macro risk news fetch failed; using cached items",
                        data={"error": str(exc)[:500]},
                    )
                )

        lookback_hours = max(1, int(_float(config.get("news_lookback_hours"), 12)))
        fresh = [
            item
            for item in _dedupe_news_items(items)
            if now - _ensure_aware(item.published_at) <= timedelta(hours=lookback_hours)
        ]
        scored = [
            {
                **_serialize_news_item(item),
                **score_news_item(item, now),
            }
            for item in fresh
        ]
        relevant = [item for item in scored if item["severity"] >= 20]
        top = sorted(relevant, key=lambda item: item["severity"], reverse=True)[:5]
        top_score = int(top[0]["severity"]) if top else 0
        aggregate_bonus = min(20, max(0, len(relevant) - 1) * 4)
        score = min(100, top_score + aggregate_bonus)
        return {
            "score": score,
            "items": top,
            "cached_items": scored[:20],
            "errors": errors,
            "fetched": fetched,
            "last_news_fetch_at": now.isoformat() if fetched else previous.get("last_news_fetch_at"),
        }

    def _calendar_score(
        self,
        db: Session,
        config: dict[str, Any],
        previous: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if not bool(config.get("calendar_enabled", True)):
            return {
                "score": 0,
                "active_events": [],
                "next_events": [],
                "errors": [],
                "fetched": False,
            }

        cooldown_hours = max(1, int(_float(config.get("calendar_fetch_cooldown_hours"), 6)))
        last_fetch = _parse_dt(previous.get("last_calendar_fetch_at"))
        cached_events = _deserialize_calendar_events(previous.get("cached_calendar_events"), now)
        static_events = _static_calendar_events(config)
        should_fetch = last_fetch is None or now - last_fetch >= timedelta(hours=cooldown_hours)
        fetched_events = cached_events
        attempted = False
        fetched = False
        errors: list[str] = []

        if should_fetch:
            attempted = True
            urls = [str(url).strip() for url in config.get("calendar_feed_urls") or [] if str(url).strip()]
            try:
                fetched_events = self.calendar_client.fetch(urls) if urls else []
                fetched = True
            except Exception as exc:
                if not static_events and not cached_events:
                    errors.append(str(exc)[:240])
                    db.add(
                        DecisionLog(
                            event_type="macro_risk_calendar_fetch_failed",
                            message="Macro risk calendar fetch failed; using cached events",
                            data={"error": str(exc)[:500]},
                        )
                    )

        events = _dedupe_calendar_events([*static_events, *fetched_events])
        before = timedelta(minutes=max(0, int(_float(config.get("calendar_pre_event_minutes"), 90))))
        after = timedelta(minutes=max(0, int(_float(config.get("calendar_post_event_minutes"), 45))))
        lookahead = timedelta(hours=max(1, int(_float(config.get("calendar_lookahead_hours"), 48))))

        scored = [
            {
                **_serialize_calendar_event(event),
                **score_calendar_event(event),
            }
            for event in events
            if _ensure_aware(event.starts_at) >= now - after
            and _ensure_aware(event.starts_at) <= now + lookahead
        ]
        active: list[dict[str, Any]] = []
        next_events: list[dict[str, Any]] = []
        for event in scored:
            starts_at = _parse_dt(event.get("starts_at"))
            if starts_at is None or int(event.get("severity") or 0) < 40:
                continue
            if now - after <= starts_at <= now + before:
                active.append(event)
            elif starts_at > now + before:
                next_events.append(event)
        next_events = next_events[:5]
        top = sorted(active, key=lambda event: int(event.get("severity") or 0), reverse=True)[:5]
        score = int(top[0]["severity"]) if top else 0
        return {
            "score": score,
            "active_events": top,
            "next_events": next_events,
            "cached_events": [_serialize_calendar_event(event) for event in events[:80]],
            "errors": errors,
            "fetched": fetched,
            "last_calendar_fetch_at": (
                now.isoformat() if attempted else previous.get("last_calendar_fetch_at")
            ),
        }

    def _market_stress_score(
        self,
        db: Session,
        config: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if not bool(config.get("market_stress_enabled", True)):
            return {"score": 0, "markets": []}

        lookback_minutes = max(30, int(_float(config.get("market_lookback_minutes"), 120)))
        since = now - timedelta(minutes=lookback_minutes)
        markets = (
            db.query(Market)
            .filter(Market.active.is_(True), Market.base.in_(MARKET_TARGET_BASES))
            .all()
        )
        market_ids = [market.id for market in markets if market.id is not None]
        if not market_ids:
            return {"score": 0, "markets": []}
        candles = (
            db.query(Candle)
            .filter(
                Candle.market_id.in_(market_ids),
                Candle.timeframe == "5m",
                Candle.open_time >= since,
            )
            .order_by(Candle.market_id, Candle.open_time)
            .all()
        )
        by_market: dict[int, list[Candle]] = {}
        for candle in candles:
            if _ensure_aware(candle.open_time) >= since:
                by_market.setdefault(candle.market_id, []).append(candle)

        details: list[dict[str, Any]] = []
        scores: list[float] = []
        for market in markets:
            rows = sorted(by_market.get(market.id, []), key=lambda candle: candle.open_time)
            score, detail = _score_market_candles(market, rows)
            if detail:
                details.append(detail)
                scores.append(score)
        if not scores:
            return {"score": 0, "markets": []}
        return {"score": int(min(100, max(scores))), "markets": details}

    def _next_state(
        self,
        *,
        config: dict[str, Any],
        previous: dict[str, Any],
        now: datetime,
        raw_level: str,
        combined_score: int,
        news_result: dict[str, Any],
        calendar_result: dict[str, Any],
        market_result: dict[str, Any],
    ) -> dict[str, Any]:
        previous_until = _parse_dt(previous.get("active_until"))
        level = raw_level
        active = level != "off"
        retained_previous_level = False
        previous_level = str(previous.get("level") or "off")
        previous_still_active = bool(previous.get("active")) and previous_until is not None and previous_until > now
        should_retain_previous = previous_still_active and (
            not active
            or (
                _is_lower_level(raw_level, previous_level)
                and not _downgrade_confirmed(
                    combined_score,
                    previous_level=previous_level,
                    config=config,
                )
            )
        )
        if should_retain_previous:
            level = previous_level if previous_level != "off" else "watch"
            active = True
            retained_previous_level = True

        ttl_minutes = _ttl_minutes(config, level)
        active_until: datetime | None
        if active and retained_previous_level and previous_until is not None:
            active_until = previous_until
        else:
            active_until = now + timedelta(minutes=ttl_minutes) if active else None
        policy = _policy_for_level(config, level) if active and bool(config.get("actions_enabled", True)) else {}
        sources: list[str] = []
        if int(news_result["score"]) >= 20:
            sources.append("news")
        if int(calendar_result["score"]) >= 40:
            sources.append("macro_calendar")
        if int(market_result["score"]) >= 30:
            sources.append("market_stress")
        reason = _reason_for(level, news_result, calendar_result, market_result)
        triggered_at = previous.get("triggered_at")
        if active and (not previous.get("active") or previous.get("level") != level):
            triggered_at = now.isoformat()
        if not active:
            triggered_at = None

        return _merged_state(
            {
                "active": active,
                "level": level,
                "reason": reason,
                "confidence": combined_score,
                "combined_score": combined_score,
                "news_score": int(news_result["score"]),
                "calendar_score": int(calendar_result["score"]),
                "market_stress_score": int(market_result["score"]),
                "triggered_at": triggered_at,
                "active_until": active_until.isoformat() if active_until is not None else None,
                "last_checked_at": now.isoformat(),
                "summary": _summary_for(level, news_result, calendar_result, market_result),
                "sources": sources,
                "policy": policy,
                "top_news": news_result["items"],
                "calendar": calendar_result,
                "market_stress": market_result,
                "last_news_fetch_at": news_result.get("last_news_fetch_at"),
                "last_calendar_fetch_at": calendar_result.get("last_calendar_fetch_at"),
                "cached_news_items": news_result.get("cached_items", previous.get("cached_news_items", [])),
                "cached_calendar_events": calendar_result.get(
                    "cached_events",
                    previous.get("cached_calendar_events", []),
                ),
                "news_fetch_errors": news_result.get("errors", []),
                "calendar_fetch_errors": calendar_result.get("errors", []),
            }
        )


def parse_feed(raw_xml: str, *, source_url: str) -> list[MacroNewsItem]:
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError:
        return []
    source = _feed_source(root, source_url)
    nodes = [node for node in root.iter() if _tag(node.tag) in {"item", "entry"}]
    items: list[MacroNewsItem] = []
    for node in nodes:
        title = _child_text(node, "title")
        if not title:
            continue
        link = _child_text(node, "link")
        if link is None:
            link_node = _child(node, "link")
            link = link_node.attrib.get("href") if link_node is not None else None
        published = (
            _child_text(node, "pubDate")
            or _child_text(node, "published")
            or _child_text(node, "updated")
        )
        published_at = _parse_published_at(published)
        summary = _child_text(node, "description") or _child_text(node, "summary")
        items.append(
            MacroNewsItem(
                title=title.strip(),
                source=source,
                url=link.strip() if isinstance(link, str) and link.strip() else None,
                published_at=published_at,
                summary=summary.strip() if isinstance(summary, str) and summary.strip() else None,
            )
        )
    return items


def parse_ics_calendar(raw_ics: str, *, source_url: str) -> list[MacroCalendarEvent]:
    source = urlparse(source_url).netloc or "calendar"
    events: list[MacroCalendarEvent] = []
    for block in _ics_event_blocks(raw_ics):
        title = block.get("SUMMARY", "").strip()
        starts_at = _parse_ics_datetime(block.get("DTSTART"))
        if not title or starts_at is None:
            continue
        url = block.get("URL") or source_url
        category, score = classify_calendar_event(title)
        if score <= 0:
            continue
        events.append(
            MacroCalendarEvent(
                title=title,
                source=source,
                starts_at=starts_at,
                category=category,
                score=score,
                url=url,
                key=block.get("UID"),
            )
        )
    return events


def classify_calendar_event(title: str) -> tuple[str, int]:
    text = title.lower()
    if any(phrase in text for phrase in ["fomc", "federal funds", "rate decision"]):
        return "macro_calendar", 72
    if any(phrase in text for phrase in ["consumer price index", "cpi"]):
        return "macro_calendar", 72
    if any(phrase in text for phrase in ["employment situation", "nonfarm payroll", "jobs report"]):
        return "macro_calendar", 66
    if any(phrase in text for phrase in ["personal income and outlays", "pce"]):
        return "macro_calendar", 66
    if "producer price index" in text or "ppi" in text:
        return "macro_calendar", 58
    if "gross domestic product" in text or "gdp" in text:
        return "macro_calendar", 62
    if any(phrase in text for phrase in ["job openings", "jolts"]):
        return "macro_calendar", 50
    if any(phrase in text for phrase in ["treasury", "refunding", "auction"]):
        return "liquidity", 55
    return "general", 0


def score_calendar_event(event: MacroCalendarEvent) -> dict[str, Any]:
    score = event.score if event.score is not None else classify_calendar_event(event.title)[1]
    return {
        "severity": int(min(100, max(0, score))),
        "category": event.category or classify_calendar_event(event.title)[0],
    }


def score_news_item(item: MacroNewsItem, now: datetime) -> dict[str, Any]:
    text = _normalized_news_text(item.title, item.summary)
    severity = 0
    category = "general"
    entities: list[str] = []
    matched_event_phrases: set[str] = set()

    entity_weights = {
        "iran": 18,
        "israel": 18,
        "united states": 12,
        "u.s.": 12,
        "hormuz": 22,
        "middle east": 12,
        "russia": 10,
        "ukraine": 10,
        "taiwan": 10,
        "china": 8,
        "federal reserve": 12,
        "fomc": 14,
        "cpi": 12,
        "pce": 12,
        "nonfarm payrolls": 14,
        "nfp": 14,
        "jobs report": 12,
        "unemployment": 10,
        "producer price index": 10,
        "ppi": 10,
        "gdp": 8,
        "powell": 8,
        "inflation": 10,
        "treasury": 10,
        "refunding": 14,
        "auction": 8,
        "debt ceiling": 18,
        "repo": 12,
        "liquidity": 10,
        "oil": 10,
        "brent": 10,
        "wti": 10,
        "opec": 10,
        "suez": 12,
        "red sea": 12,
        "bank": 8,
        "regional bank": 14,
        "credit suisse": 14,
        "deutsche bank": 12,
        "svb": 12,
        "sec": 8,
        "cftc": 8,
        "doj": 8,
        "etf": 8,
        "stablecoin": 14,
        "tether": 14,
        "usdt": 14,
        "usdc": 12,
        "binance": 10,
        "coinbase": 10,
        "hyperliquid": 14,
        "kraken": 8,
        "bybit": 8,
        "okx": 8,
    }
    event_weights = {
        "airstrike": 24,
        "strike": 20,
        "missile": 22,
        "retaliation": 20,
        "war": 18,
        "attack": 16,
        "bomb": 16,
        "nuclear": 24,
        "ceasefire": 12,
        "talks collapse": 24,
        "peace talks": 12,
        "sanctions": 14,
        "strait of hormuz": 28,
        "emergency": 12,
        "rate hike": 18,
        "rate cut": 12,
        "higher for longer": 18,
        "hawkish": 14,
        "hot inflation": 18,
        "payrolls beat": 12,
        "payrolls miss": 12,
        "jobs data": 10,
        "auction tails": 18,
        "weak auction": 18,
        "yield spike": 16,
        "liquidity crunch": 20,
        "funding stress": 18,
        "debt limit": 16,
        "debt ceiling": 18,
        "hack": 22,
        "exploit": 20,
        "bridge exploit": 24,
        "depeg": 28,
        "de-pegged": 28,
        "insolvency": 24,
        "bankruptcy": 22,
        "withdrawals halted": 24,
        "halt withdrawals": 24,
        "chain halt": 22,
        "network halt": 22,
        "outage": 14,
        "liquidation": 16,
        "mass liquidations": 22,
        "enforcement": 14,
        "lawsuit": 12,
        "ban": 14,
        "sanctioned": 16,
        "approval": 8,
        "rejection": 10,
        "rate decision": 14,
        "oil spike": 18,
        "supply disruption": 18,
        "bank failure": 24,
        "bank run": 24,
        "emergency lending": 18,
        "credit spread": 12,
    }

    for phrase, weight in entity_weights.items():
        if _normalized_phrase_in_text(text, phrase):
            severity += weight
            entities.append(phrase.strip())
    for phrase, weight in event_weights.items():
        if _normalized_phrase_in_text(text, phrase):
            severity += weight
            matched_event_phrases.add(phrase)

    entity_set = set(entities)
    overlap_deductions = {
        "strait of hormuz": ["hormuz"],
        "debt ceiling": ["debt ceiling"],
        "bank failure": ["bank"],
        "bank run": ["bank"],
        "weak auction": ["auction"],
        "auction tails": ["auction"],
    }
    for event_phrase, overlapped_entities in overlap_deductions.items():
        if event_phrase not in matched_event_phrases:
            continue
        for overlapped in overlapped_entities:
            if overlapped in entity_set:
                severity -= entity_weights.get(overlapped, 0)

    if any(entity in entities for entity in ["iran", "israel", "hormuz", "middle east"]):
        category = "geopolitical"
    elif any(
        _normalized_phrase_in_text(text, phrase)
        for phrase in [
            "bank failure",
            "bank run",
            "emergency lending",
            "regional bank",
            "credit spread",
        ]
    ):
        category = "banking_credit"
    elif any(
        _normalized_phrase_in_text(text, phrase)
        for phrase in [
            "stablecoin",
            "tether",
            "usdt",
            "usdc",
            "depeg",
            "de-pegged",
            "hack",
            "exploit",
            "withdrawals halted",
            "chain halt",
            "network halt",
        ]
    ) or any(entity in entities for entity in ["binance", "coinbase", "hyperliquid"]):
        category = "crypto_systemic"
    elif any(entity in entities for entity in ["sec", "cftc", "doj"]) or any(
        _normalized_phrase_in_text(text, phrase)
        for phrase in ["enforcement", "lawsuit", "ban", "sanctioned"]
    ):
        category = "crypto_regulatory"
    elif any(
        entity in entities
        for entity in ["treasury", "refunding", "auction", "debt ceiling", "repo", "liquidity"]
    ) or any(
        _normalized_phrase_in_text(text, phrase)
        for phrase in ["weak auction", "auction tails", "yield spike", "funding stress"]
    ):
        category = "liquidity"
    elif any(entity in entities for entity in ["oil", "brent", "wti", "opec", "suez", "red sea"]):
        category = "energy"
    elif any(
        entity in entities
        for entity in [
            "federal reserve",
            "fomc",
            "cpi",
            "pce",
            "nonfarm payrolls",
            "nfp",
            "jobs report",
            "unemployment",
            "producer price index",
            "ppi",
            "gdp",
            "inflation",
        ]
    ):
        category = "macro_calendar"

    if category == "general":
        severity = int(severity * 0.4)

    age = now - _ensure_aware(item.published_at)
    if age <= timedelta(hours=1):
        decay = 1.0
    elif age <= timedelta(hours=4):
        decay = 0.8
    elif age <= timedelta(hours=8):
        decay = 0.6
    elif age <= timedelta(hours=24):
        decay = 0.35
    else:
        decay = 0.1

    return {
        "severity": int(min(100, max(0, severity * decay))),
        "category": category,
        "entities": sorted(set(entities))[:8],
        "age_minutes": int(max(0, age.total_seconds() // 60)),
    }


def _normalized_news_text(title: str, summary: str | None) -> str:
    combined = f"{title} {summary or ''}"
    # RSS descriptions often contain HTML and embedded link URLs; leaving them
    # in place creates false positives like `ppi` inside `shipping` or keyword
    # hits from href slugs rather than visible article text.
    plain = re.sub(r"<[^>]+>", " ", combined)
    plain = unescape(plain)
    plain = re.sub(r"https?://\S+", " ", plain)
    normalized = re.sub(r"[^a-z0-9]+", " ", plain.lower())
    return normalized.strip()


def _normalized_phrase_in_text(text: str, phrase: str) -> bool:
    needle = re.sub(r"[^a-z0-9]+", " ", phrase.lower()).strip()
    if not needle:
        return False
    haystack = f" {text} "
    return f" {needle} " in haystack


def _score_market_candles(market: Market, candles: list[Candle]) -> tuple[float, dict[str, Any] | None]:
    if len(candles) < 6:
        return 0.0, None
    first_open = _decimal(candles[0].open)
    last_close = _decimal(candles[-1].close)
    if first_open <= 0:
        return 0.0, None
    return_pct = abs((last_close - first_open) / first_open * Decimal("100"))
    ranges = [
        abs((_decimal(candle.high) - _decimal(candle.low)) / max(_decimal(candle.open), Decimal("1e-9")) * Decimal("100"))
        for candle in candles
    ]
    max_range_pct = max(ranges) if ranges else Decimal("0")
    volumes = [float(_decimal(candle.volume)) for candle in candles if _decimal(candle.volume) > 0]
    recent_volume = sum(volumes[-3:]) / min(3, len(volumes)) if volumes else 0.0
    baseline = median(volumes[:-3]) if len(volumes) > 6 else median(volumes) if volumes else 0.0
    volume_ratio = recent_volume / baseline if baseline > 0 else 1.0
    score = (
        float(return_pct) * 18
        + float(max_range_pct) * 14
        + max(0.0, volume_ratio - 1.0) * 18
    )
    detail = {
        "symbol": market.symbol,
        "base": market.base,
        "timeframe": "5m",
        "candles": len(candles),
        "return_pct": round(float(return_pct), 4),
        "max_range_pct": round(float(max_range_pct), 4),
        "volume_ratio": round(float(volume_ratio), 4),
        "score": int(min(100, max(0, score))),
    }
    return min(100, max(0, score)), detail


def _combined_score(news_score: int, calendar_score: int, market_score: int) -> int:
    event_score = max(news_score, calendar_score)
    score = event_score * 0.55 + market_score * 0.45
    if event_score >= 70 and market_score >= 25:
        score += 8
    if calendar_score >= 65:
        score = max(score, 60)
    if market_score >= 85 and event_score < 35:
        score = max(score, 55)
    return int(min(100, max(0, round(score))))


def _level_for_score(score: int, config: dict[str, Any]) -> str:
    if score >= int(_float(config.get("extreme_score"), 80)):
        return "extreme"
    if score >= int(_float(config.get("high_score"), 60)):
        return "high"
    if score >= int(_float(config.get("watch_score"), 40)):
        return "watch"
    return "off"


def _is_lower_level(candidate: str, current: str) -> bool:
    return LEVEL_ORDER.get(candidate, 0) < LEVEL_ORDER.get(current, 0)


def _downgrade_confirmed(score: int, *, previous_level: str, config: dict[str, Any]) -> bool:
    hysteresis = max(0, int(_float(config.get("level_hysteresis_points"), 5)))
    if previous_level == "extreme":
        return score < int(_float(config.get("extreme_score"), 80)) - hysteresis
    if previous_level == "high":
        return score < int(_float(config.get("high_score"), 60)) - hysteresis
    if previous_level == "watch":
        return score < int(_float(config.get("watch_score"), 40)) - hysteresis
    return True


def _policy_for_level(config: dict[str, Any], level: str) -> dict[str, Any]:
    policies = config.get("policies") if isinstance(config.get("policies"), dict) else {}
    policy = policies.get(level) if isinstance(policies, dict) else {}
    return dict(policy) if isinstance(policy, dict) else {}


def _ttl_minutes(config: dict[str, Any], level: str) -> int:
    ttl = config.get("ttl_minutes_by_level")
    if isinstance(ttl, dict):
        return max(1, int(_float(ttl.get(level), 120)))
    return 120


def _reason_for(
    level: str,
    news_result: dict[str, Any],
    calendar_result: dict[str, Any],
    market_result: dict[str, Any],
) -> str | None:
    if level == "off":
        return None
    reasons: list[str] = []
    active_events = calendar_result.get("active_events") or []
    if active_events:
        category = active_events[0].get("category")
        if category == "liquidity":
            reasons.append("treasury_liquidity_risk")
        else:
            reasons.append("macro_calendar_risk")
    top_news = news_result.get("items") or []
    if top_news:
        category = top_news[0].get("category")
        if category == "geopolitical":
            reasons.append("geopolitical_escalation")
        elif category == "crypto_systemic":
            reasons.append("crypto_systemic_risk")
        elif category == "crypto_regulatory":
            reasons.append("crypto_regulatory_risk")
        elif category == "macro_calendar":
            reasons.append("macro_calendar_risk")
        elif category == "liquidity":
            reasons.append("treasury_liquidity_risk")
        elif category == "energy":
            reasons.append("energy_shock_risk")
        elif category == "banking_credit":
            reasons.append("banking_credit_stress")
        else:
            reasons.append("headline_risk")
    if int(market_result.get("score") or 0) >= 30:
        reasons.append("market_stress")
    deduped = list(dict.fromkeys(reasons))
    return ", ".join(deduped) if deduped else "macro_risk"


def _summary_for(
    level: str,
    news_result: dict[str, Any],
    calendar_result: dict[str, Any],
    market_result: dict[str, Any],
) -> str:
    if level == "off":
        return "Macro risk normal"
    headline = None
    top_news = news_result.get("items") or []
    if top_news:
        headline = top_news[0].get("title")
    active_events = calendar_result.get("active_events") or []
    calendar_title = active_events[0].get("title") if active_events else None
    market_score = int(market_result.get("score") or 0)
    parts = [f"Macro risk {level.upper()}"]
    if calendar_title:
        parts.append(str(calendar_title)[:120])
    if headline:
        parts.append(str(headline)[:160])
    if market_score:
        parts.append(f"market stress {market_score}/100")
    return " · ".join(parts)


def _state_changed(previous: dict[str, Any], state: dict[str, Any]) -> bool:
    return (
        bool(previous.get("active")) != bool(state.get("active"))
        or str(previous.get("level") or "off") != str(state.get("level") or "off")
    )


def _log_transition(
    db: Session,
    previous: dict[str, Any],
    state: dict[str, Any],
    *,
    status: str,
) -> None:
    db.add(
        DecisionLog(
            event_type="macro_risk_state_changed",
            message=f"Macro risk state changed to {state.get('level')}",
            data={
                "status": status,
                "previous_level": previous.get("level", "off"),
                "level": state.get("level"),
                "active": state.get("active"),
                "reason": state.get("reason"),
                "active_until": state.get("active_until"),
                "combined_score": state.get("combined_score"),
                "news_score": state.get("news_score"),
                "calendar_score": state.get("calendar_score"),
                "market_stress_score": state.get("market_stress_score"),
                "policy": state.get("policy"),
                "summary": state.get("summary"),
            },
        )
    )


def _upsert_state(db: Session, row: AppSetting | None, state: dict[str, Any]) -> None:
    if row is None:
        db.add(AppSetting(key="macro_risk_state", value=state))
    else:
        row.value = state


def _merged_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    state = dict(DEFAULT_MACRO_RISK_STATE)
    if isinstance(raw, dict):
        state.update(raw)
    return state


def _serialize_news_item(item: MacroNewsItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "source": item.source,
        "url": item.url,
        "published_at": _ensure_aware(item.published_at).isoformat(),
        "summary": item.summary,
    }


def _serialize_calendar_event(event: MacroCalendarEvent) -> dict[str, Any]:
    return {
        "key": event.key,
        "title": event.title,
        "source": event.source,
        "url": event.url,
        "starts_at": _ensure_aware(event.starts_at).isoformat(),
        "category": event.category,
        "score": event.score,
    }


def _deserialize_news_items(raw: Any, now: datetime) -> list[MacroNewsItem]:
    if not isinstance(raw, list):
        return []
    items: list[MacroNewsItem] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(
            MacroNewsItem(
                title=title,
                source=str(row.get("source") or "cached"),
                url=str(row.get("url")) if row.get("url") else None,
                published_at=_parse_dt(row.get("published_at")) or now,
                summary=str(row.get("summary")) if row.get("summary") else None,
            )
        )
    return items


def _deserialize_calendar_events(raw: Any, now: datetime) -> list[MacroCalendarEvent]:
    if not isinstance(raw, list):
        return []
    events: list[MacroCalendarEvent] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        starts_at = _parse_dt(row.get("starts_at")) or now
        score_value = row.get("score")
        score = int(_float(score_value, 0)) if score_value is not None else None
        events.append(
            MacroCalendarEvent(
                title=title,
                source=str(row.get("source") or "calendar"),
                url=str(row.get("url")) if row.get("url") else None,
                starts_at=starts_at,
                category=str(row.get("category") or "macro_calendar"),
                score=score,
                key=str(row.get("key")) if row.get("key") else None,
            )
        )
    return events


def _dedupe_news_items(items: list[MacroNewsItem]) -> list[MacroNewsItem]:
    seen: set[str] = set()
    out: list[MacroNewsItem] = []
    for item in sorted(items, key=lambda row: row.published_at, reverse=True):
        key = item.url or item.title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _dedupe_calendar_events(events: list[MacroCalendarEvent]) -> list[MacroCalendarEvent]:
    seen: set[str] = set()
    out: list[MacroCalendarEvent] = []
    for event in sorted(events, key=lambda row: _ensure_aware(row.starts_at)):
        key = event.key or f"{event.title.lower()}:{_ensure_aware(event.starts_at).isoformat()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _static_calendar_events(config: dict[str, Any]) -> list[MacroCalendarEvent]:
    raw_events = config.get("calendar_static_events")
    if not isinstance(raw_events, list):
        return []
    events: list[MacroCalendarEvent] = []
    for row in raw_events:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        starts_at = _parse_dt(row.get("starts_at"))
        if not title or starts_at is None:
            continue
        score_value = row.get("score")
        category = str(row.get("category") or classify_calendar_event(title)[0])
        score = int(_float(score_value, classify_calendar_event(title)[1]))
        if score <= 0:
            continue
        events.append(
            MacroCalendarEvent(
                title=title,
                source=str(row.get("source") or "calendar"),
                url=str(row.get("url")) if row.get("url") else None,
                starts_at=starts_at,
                category=category,
                score=score,
                key=str(row.get("key")) if row.get("key") else None,
            )
        )
    return events


def _ics_event_blocks(raw_ics: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics_lines(raw_ics):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key_raw, value = line.split(":", 1)
        key = key_raw.split(";", 1)[0].upper()
        current[key] = _unescape_ics(value.strip())
    return events


def _unfold_ics_lines(raw_ics: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw_ics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        elif raw_line:
            lines.append(raw_line.strip())
    return lines


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ics_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        if raw.endswith("Z"):
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        elif "T" in raw:
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=CALENDAR_DEFAULT_TZ)
        else:
            parsed = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=CALENDAR_DEFAULT_TZ)
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _feed_source(root: ElementTree.Element, source_url: str) -> str:
    title = _child_text(root, "title")
    if title:
        return title.strip()[:80]
    channel = _child(root, "channel")
    if channel is not None:
        title = _child_text(channel, "title")
        if title:
            return title.strip()[:80]
    host = urlparse(source_url).netloc
    return host or "rss"


def _child(node: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    for child in node:
        if _tag(child.tag) == tag:
            return child
    return None


def _child_text(node: ElementTree.Element, tag: str) -> str | None:
    child = _child(node, tag)
    if child is None or child.text is None:
        return None
    return child.text


def _tag(raw: str) -> str:
    return raw.rsplit("}", 1)[-1]


def _parse_published_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed: datetime | None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = _parse_dt(value)
    if parsed is None:
        return datetime.now(UTC)
    return _ensure_aware(parsed)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        try:
            return _ensure_aware(parsedate_to_datetime(value))
        except (TypeError, ValueError):
            return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
