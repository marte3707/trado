from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from statistics import median
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

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
    ) -> None:
        self.settings = settings
        self.news_client = news_client or MacroNewsClient()

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
            return {"status": "disabled", "changed": changed, "state": state}

        news_result = self._news_score(db, config, previous, now)
        market_result = self._market_stress_score(db, config, now)
        combined_score = _combined_score(news_result["score"], market_result["score"])
        raw_level = _level_for_score(combined_score, config)
        state = self._next_state(
            config=config,
            previous=previous,
            now=now,
            raw_level=raw_level,
            combined_score=combined_score,
            news_result=news_result,
            market_result=market_result,
        )
        changed = _state_changed(previous, state)
        _upsert_state(db, state_row, state)
        if changed:
            _log_transition(db, previous, state, status="updated")
        return {"status": "updated", "changed": changed, "state": state}

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
        market_result: dict[str, Any],
    ) -> dict[str, Any]:
        previous_until = _parse_dt(previous.get("active_until"))
        level = raw_level
        active = level != "off"
        if not active and previous.get("active") and previous_until is not None and previous_until > now:
            level = str(previous.get("level") or "watch")
            active = True

        ttl_minutes = _ttl_minutes(config, level)
        active_until = now + timedelta(minutes=ttl_minutes) if active else None
        policy = _policy_for_level(config, level) if active and bool(config.get("actions_enabled", True)) else {}
        sources: list[str] = []
        if int(news_result["score"]) >= 20:
            sources.append("news")
        if int(market_result["score"]) >= 30:
            sources.append("market_stress")
        reason = _reason_for(level, news_result, market_result)
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
                "market_stress_score": int(market_result["score"]),
                "triggered_at": triggered_at,
                "active_until": active_until.isoformat() if active_until is not None else None,
                "last_checked_at": now.isoformat(),
                "summary": _summary_for(level, news_result, market_result),
                "sources": sources,
                "policy": policy,
                "top_news": news_result["items"],
                "market_stress": market_result,
                "last_news_fetch_at": news_result.get("last_news_fetch_at"),
                "cached_news_items": news_result.get("cached_items", previous.get("cached_news_items", [])),
                "news_fetch_errors": news_result.get("errors", []),
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


def score_news_item(item: MacroNewsItem, now: datetime) -> dict[str, Any]:
    text = f"{item.title} {item.summary or ''}".lower()
    severity = 0
    category = "general"
    entities: list[str] = []

    entity_weights = {
        "iran": 18,
        "israel": 18,
        "united states": 12,
        "u.s.": 12,
        "us ": 10,
        "hormuz": 22,
        "middle east": 12,
        "russia": 10,
        "ukraine": 10,
        "taiwan": 10,
        "china": 8,
        "federal reserve": 12,
        "fomc": 14,
        "cpi": 12,
        "inflation": 10,
        "sec": 8,
        "binance": 10,
        "coinbase": 10,
        "hyperliquid": 14,
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
        "hack": 22,
        "exploit": 20,
        "liquidation": 16,
        "outage": 10,
        "etf": 8,
        "rate decision": 14,
        "hot inflation": 18,
    }

    for phrase, weight in entity_weights.items():
        if phrase in text:
            severity += weight
            entities.append(phrase.strip())
    for phrase, weight in event_weights.items():
        if phrase in text:
            severity += weight

    if any(entity in entities for entity in ["iran", "israel", "hormuz", "middle east"]):
        category = "geopolitical"
    elif any(entity in entities for entity in ["federal reserve", "fomc", "cpi", "inflation"]):
        category = "macro_calendar"
    elif any(entity in entities for entity in ["binance", "coinbase", "hyperliquid"]):
        category = "crypto_systemic"

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


def _combined_score(news_score: int, market_score: int) -> int:
    score = news_score * 0.55 + market_score * 0.45
    if news_score >= 70 and market_score >= 25:
        score += 8
    if market_score >= 85 and news_score < 35:
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


def _policy_for_level(config: dict[str, Any], level: str) -> dict[str, Any]:
    policies = config.get("policies") if isinstance(config.get("policies"), dict) else {}
    policy = policies.get(level) if isinstance(policies, dict) else {}
    return dict(policy) if isinstance(policy, dict) else {}


def _ttl_minutes(config: dict[str, Any], level: str) -> int:
    ttl = config.get("ttl_minutes_by_level")
    if isinstance(ttl, dict):
        return max(1, int(_float(ttl.get(level), 120)))
    return 120


def _reason_for(level: str, news_result: dict[str, Any], market_result: dict[str, Any]) -> str | None:
    if level == "off":
        return None
    reasons: list[str] = []
    top_news = news_result.get("items") or []
    if top_news:
        category = top_news[0].get("category")
        if category == "geopolitical":
            reasons.append("geopolitical_escalation")
        elif category == "crypto_systemic":
            reasons.append("crypto_systemic_risk")
        elif category == "macro_calendar":
            reasons.append("macro_calendar_risk")
        else:
            reasons.append("headline_risk")
    if int(market_result.get("score") or 0) >= 30:
        reasons.append("market_stress")
    return ", ".join(reasons) if reasons else "macro_risk"


def _summary_for(level: str, news_result: dict[str, Any], market_result: dict[str, Any]) -> str:
    if level == "off":
        return "Macro risk normal"
    headline = None
    top_news = news_result.get("items") or []
    if top_news:
        headline = top_news[0].get("title")
    market_score = int(market_result.get("score") or 0)
    parts = [f"Macro risk {level.upper()}"]
    if headline:
        parts.append(str(headline)[:160])
    if market_score:
        parts.append(f"market stress {market_score}/100")
    return " · ".join(parts)


def _state_changed(previous: dict[str, Any], state: dict[str, Any]) -> bool:
    return (
        bool(previous.get("active")) != bool(state.get("active"))
        or str(previous.get("level") or "off") != str(state.get("level") or "off")
        or str(previous.get("reason") or "") != str(state.get("reason") or "")
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
