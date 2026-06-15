from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import AppSetting, Candle, DecisionLog, Market
from app.engine.macro_risk_monitor import (
    MacroCalendarEvent,
    MacroNewsItem,
    MacroRiskMonitor,
    _state_changed,
    parse_feed,
    parse_ics_calendar,
    score_news_item,
)


@dataclass
class FakeQuery:
    rows: list[Any]

    def filter(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        return self

    def all(self) -> list[Any]:
        return list(self.rows)


class FakeDb:
    def __init__(
        self,
        *,
        settings: dict[str, dict[str, Any]] | None = None,
        markets: list[Market] | None = None,
        candles: list[Candle] | None = None,
    ) -> None:
        self.settings = {
            key: AppSetting(key=key, value=value)
            for key, value in (settings or {}).items()
        }
        self.markets = markets or []
        self.candles = candles or []
        self.added: list[Any] = []

    def get(self, model: type[Any], key: Any) -> Any | None:
        if model is AppSetting:
            return self.settings.get(str(key))
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, AppSetting):
            self.settings[row.key] = row

    def query(self, model: type[Any], *_args: Any, **_kwargs: Any) -> FakeQuery:
        if model is Market:
            return FakeQuery(self.markets)
        if model is Candle:
            return FakeQuery(self.candles)
        if model is DecisionLog:
            return FakeQuery([row for row in self.added if isinstance(row, DecisionLog)])
        return FakeQuery([])


class StaticNewsClient:
    def __init__(self, items: list[MacroNewsItem] | None = None, *, error: Exception | None = None) -> None:
        self.items = items or []
        self.error = error
        self.calls = 0

    def fetch(self, _urls: list[str]) -> list[MacroNewsItem]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.items)


class StaticCalendarClient:
    def __init__(self, events: list[MacroCalendarEvent] | None = None, *, error: Exception | None = None) -> None:
        self.events = events or []
        self.error = error
        self.calls = 0

    def fetch(self, _urls: list[str]) -> list[MacroCalendarEvent]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.events)


def _risk_settings(**monitor_overrides: Any) -> dict[str, Any]:
    return {
        "min_signal_score": 65,
        "macro_risk_monitor": {
            "enabled": True,
            "actions_enabled": True,
            "news_enabled": True,
            "calendar_enabled": False,
            "market_stress_enabled": True,
            "news_feed_urls": ["https://example.com/rss.xml"],
            "news_fetch_cooldown_minutes": 15,
            "news_lookback_hours": 12,
            "market_lookback_minutes": 120,
            "watch_score": 40,
            "high_score": 60,
            "extreme_score": 80,
            "ttl_minutes_by_level": {"watch": 120, "high": 360, "extreme": 720},
            "policies": {
                "watch": {
                    "size_multiplier": 1.0,
                    "min_score_add": 4,
                    "disable_timeframes": [],
                    "require_mtf_confluence": False,
                },
                "high": {
                    "size_multiplier": 0.5,
                    "min_score_add": 8,
                    "disable_timeframes": ["5m"],
                    "require_mtf_confluence": False,
                },
                "extreme": {
                    "size_multiplier": 0.25,
                    "min_score_add": 12,
                    "disable_timeframes": ["5m"],
                    "require_mtf_confluence": True,
                },
            },
            **monitor_overrides,
        },
    }


def _market() -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={},
    )


def _candles(now: datetime) -> list[Candle]:
    rows: list[Candle] = []
    prices = ["100", "100.4", "100.8", "101.4", "102.0", "102.5", "103.0"]
    for idx, price in enumerate(prices):
        open_price = Decimal(price)
        rows.append(
            Candle(
                market_id=1,
                timeframe="5m",
                open_time=now - timedelta(minutes=(len(prices) - idx) * 5),
                open=open_price,
                high=open_price * Decimal("1.012"),
                low=open_price * Decimal("0.995"),
                close=open_price * Decimal("1.004"),
                volume=Decimal("1000") if idx < 4 else Decimal("3000"),
            )
        )
    return rows


def test_parse_feed_extracts_rss_items() -> None:
    raw = """
    <rss><channel><title>World</title>
      <item>
        <title>Iran and Israel talks collapse after missile strike</title>
        <link>https://example.com/a</link>
        <pubDate>Thu, 11 Jun 2026 08:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    items = parse_feed(raw, source_url="https://example.com/rss.xml")

    assert len(items) == 1
    assert items[0].source == "World"
    assert items[0].published_at.tzinfo is not None


def test_parse_ics_calendar_extracts_high_impact_events() -> None:
    raw = """
BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-2026-07
DTSTART:20260714T083000
SUMMARY:Consumer Price Index
URL:https://www.bls.gov/schedule/news_release/cpi.htm
END:VEVENT
BEGIN:VEVENT
UID:low-impact
DTSTART:20260715T083000
SUMMARY:Regional statistics notice
END:VEVENT
END:VCALENDAR
"""

    events = parse_ics_calendar(raw, source_url="https://www.bls.gov/schedule/news_release/bls.ics")

    assert len(events) == 1
    assert events[0].title == "Consumer Price Index"
    assert events[0].score == 72
    assert events[0].starts_at.tzinfo is not None


def test_news_scoring_identifies_geopolitical_escalation() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    item = MacroNewsItem(
        title="Iran and Israel peace talks collapse after missile strike near Hormuz",
        source="test",
        url="https://example.com/a",
        published_at=now - timedelta(minutes=10),
    )

    scored = score_news_item(item, now)

    assert scored["category"] == "geopolitical"
    assert scored["severity"] >= 80


def test_news_scoring_identifies_crypto_systemic_risk() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    item = MacroNewsItem(
        title="Major stablecoin USDT depeg after exchange withdrawals halted",
        source="test",
        url="https://example.com/a",
        published_at=now - timedelta(minutes=5),
    )

    scored = score_news_item(item, now)

    assert scored["category"] == "crypto_systemic"
    assert scored["severity"] >= 60


def test_news_scoring_identifies_liquidity_risk() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    item = MacroNewsItem(
        title="Treasury weak auction tails as yields spike and liquidity worsens",
        source="test",
        url="https://example.com/a",
        published_at=now - timedelta(minutes=5),
    )

    scored = score_news_item(item, now)

    assert scored["category"] == "liquidity"
    assert scored["severity"] >= 45


def test_news_scoring_ignores_html_href_and_substring_false_positives() -> None:
    now = datetime(2026, 6, 12, 7, 11, tzinfo=UTC)
    item = MacroNewsItem(
        title="UK economy shrank by 0.1% in April as Iran war held back growth",
        source="test",
        url="https://example.com/a",
        published_at=now - timedelta(minutes=39),
        summary=(
            '<p>GDP hit by higher energy prices caused by Middle East conflict.</p>'
            '<p>As energy prices have risen as a result of the conflict, after Iran '
            'closed off the strait of Hormuz – a vital shipping route for global trade '
            '– the UK’s strong expansion slid into reverse.</p>'
            '<a href="https://example.com/us-israel-strikes-iran-oil-price">more</a>'
        ),
    )

    scored = score_news_item(item, now)

    assert scored["category"] == "geopolitical"
    assert scored["severity"] < 100
    assert "ppi" not in scored["entities"]
    assert "israel" not in scored["entities"]


def test_news_scoring_ignores_generic_event_words_without_macro_anchor() -> None:
    now = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    item = MacroNewsItem(
        title="NSW lifts drone ban over Sydney beach after shark attack",
        source="test",
        url="https://example.com/a",
        published_at=now - timedelta(minutes=5),
    )

    scored = score_news_item(item, now)

    # "ban" and "attack" match generic event keywords but there is no relevant
    # entity, so the item must stay "general" and well below the 20-pt relevance
    # threshold instead of activating crypto/geopolitical risk.
    assert scored["category"] == "general"
    assert scored["severity"] < 20


def test_monitor_activates_extreme_with_news_and_market_confirmation() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    news = [
        MacroNewsItem(
            title="Iran and Israel peace talks collapse after missile strike near Hormuz",
            source="test",
            url="https://example.com/a",
            published_at=now - timedelta(minutes=5),
        )
    ]
    db = FakeDb(
        settings={"risk": _risk_settings()},
        markets=[_market()],
        candles=_candles(now),
    )
    monitor = MacroRiskMonitor(Settings(), news_client=StaticNewsClient(news))

    result = monitor.run(db, now=now)  # type: ignore[arg-type]

    assert result["changed"] is True
    state = db.settings["macro_risk_state"].value
    assert state["active"] is True
    assert state["level"] == "extreme"
    assert state["policy"]["size_multiplier"] == 0.25
    assert state["policy"]["disable_timeframes"] == ["5m"]
    assert any(isinstance(row, DecisionLog) for row in db.added)


def test_monitor_activates_high_for_macro_calendar_window() -> None:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    calendar_event = MacroCalendarEvent(
        title="FOMC rate decision",
        source="Federal Reserve",
        url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        starts_at=now + timedelta(minutes=45),
        category="macro_calendar",
        score=72,
    )
    db = FakeDb(
        settings={
            "risk": _risk_settings(
                calendar_enabled=True,
                calendar_feed_urls=["https://example.com/calendar.ics"],
                calendar_pre_event_minutes=90,
                calendar_post_event_minutes=45,
            )
        },
        markets=[],
        candles=[],
    )
    monitor = MacroRiskMonitor(
        Settings(),
        news_client=StaticNewsClient([]),
        calendar_client=StaticCalendarClient([calendar_event]),
    )

    result = monitor.run(db, now=now)  # type: ignore[arg-type]

    state = result["state"]
    assert state["active"] is True
    assert state["level"] == "high"
    assert state["calendar_score"] == 72
    assert "macro_calendar" in state["sources"]
    assert state["reason"] == "macro_calendar_risk"
    assert state["policy"]["size_multiplier"] == 0.5


def test_monitor_suppresses_optional_calendar_feed_error_when_static_events_exist() -> None:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    db = FakeDb(
        settings={
            "risk": _risk_settings(
                calendar_enabled=True,
                calendar_feed_urls=["https://www.bls.gov/schedule/news_release/bls.ics"],
                calendar_static_events=[
                    {
                        "key": "fomc_test",
                        "title": "FOMC rate decision",
                        "category": "macro_calendar",
                        "source": "Federal Reserve",
                        "starts_at": (now + timedelta(minutes=30)).isoformat(),
                        "score": 72,
                        "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                    }
                ],
                calendar_pre_event_minutes=90,
                calendar_post_event_minutes=45,
            )
        },
        markets=[],
        candles=[],
    )
    monitor = MacroRiskMonitor(
        Settings(),
        news_client=StaticNewsClient([]),
        calendar_client=StaticCalendarClient(error=RuntimeError("403 Forbidden")),
    )

    result = monitor.run(db, now=now)  # type: ignore[arg-type]

    state = result["state"]
    assert state["level"] == "high"
    assert state["calendar_score"] == 72
    assert state["calendar_fetch_errors"] == []
    assert state["last_calendar_fetch_at"] == now.isoformat()
    assert not any(
        isinstance(row, DecisionLog) and row.event_type == "macro_risk_calendar_fetch_failed"
        for row in db.added
    )


def test_monitor_keeps_previous_extreme_inside_hysteresis_without_extending_ttl() -> None:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    active_until = now + timedelta(hours=1)
    monitor = MacroRiskMonitor(Settings(), news_client=StaticNewsClient([]))
    config = _risk_settings(level_hysteresis_points=5)["macro_risk_monitor"]

    state = monitor._next_state(  # type: ignore[attr-defined]
        config=config,
        previous={
            "active": True,
            "level": "extreme",
            "active_until": active_until.isoformat(),
            "triggered_at": (now - timedelta(minutes=30)).isoformat(),
        },
        now=now,
        raw_level="high",
        combined_score=79,
        news_result={"score": 71, "items": [], "errors": []},
        calendar_result={"score": 0, "active_events": [], "next_events": []},
        market_result={"score": 71, "markets": []},
    )

    assert state["level"] == "extreme"
    assert state["active_until"] == active_until.isoformat()


def test_monitor_keeps_previous_level_until_min_hold_elapses() -> None:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    active_until = now + timedelta(hours=1)
    monitor = MacroRiskMonitor(Settings(), news_client=StaticNewsClient([]))
    config = _risk_settings(level_hysteresis_points=5, min_level_hold_minutes=15)["macro_risk_monitor"]

    state = monitor._next_state(  # type: ignore[attr-defined]
        config=config,
        previous={
            "active": True,
            "level": "extreme",
            "active_until": active_until.isoformat(),
            "triggered_at": (now - timedelta(minutes=5)).isoformat(),
        },
        now=now,
        raw_level="high",
        combined_score=61,
        news_result={"score": 61, "items": [], "errors": []},
        calendar_result={"score": 0, "active_events": [], "next_events": []},
        market_result={"score": 61, "markets": []},
    )

    assert state["level"] == "extreme"
    assert state["active_until"] == active_until.isoformat()


def test_monitor_preserves_macro_risk_telegram_cooldown_fields() -> None:
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    active_until = now + timedelta(hours=1)
    monitor = MacroRiskMonitor(Settings(), news_client=StaticNewsClient([]))
    config = _risk_settings()["macro_risk_monitor"]

    state = monitor._next_state(  # type: ignore[attr-defined]
        config=config,
        previous={
            "active": True,
            "level": "high",
            "active_until": active_until.isoformat(),
            "triggered_at": (now - timedelta(minutes=20)).isoformat(),
            "last_telegram_risk_notification_at": (now - timedelta(minutes=3)).isoformat(),
            "last_telegram_risk_notification_level": "high",
        },
        now=now,
        raw_level="high",
        combined_score=70,
        news_result={"score": 70, "items": [], "errors": []},
        calendar_result={"score": 0, "active_events": [], "next_events": []},
        market_result={"score": 70, "markets": []},
    )

    assert state["last_telegram_risk_notification_at"] == (now - timedelta(minutes=3)).isoformat()
    assert state["last_telegram_risk_notification_level"] == "high"


def test_macro_risk_reason_only_change_is_not_state_change() -> None:
    previous = {"active": True, "level": "high", "reason": "geopolitical_escalation"}
    current = {"active": True, "level": "high", "reason": "geopolitical_escalation, market_stress"}

    assert _state_changed(previous, current) is False


def test_monitor_reuses_cached_news_during_fetch_cooldown() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    cached = {
        "title": "Iran missile strike increases Middle East risk",
        "source": "cache",
        "url": "https://example.com/a",
        "published_at": (now - timedelta(minutes=5)).isoformat(),
        "summary": None,
    }
    db = FakeDb(
        settings={
            "risk": _risk_settings(),
            "macro_risk_state": {
                "last_news_fetch_at": (now - timedelta(minutes=3)).isoformat(),
                "cached_news_items": [cached],
            },
        },
        markets=[_market()],
        candles=[],
    )
    client = StaticNewsClient([])
    monitor = MacroRiskMonitor(Settings(), news_client=client)

    result = monitor.run(db, now=now)  # type: ignore[arg-type]

    assert client.calls == 0
    assert result["state"]["news_score"] > 0


def test_market_stress_ema_smooths_spike_on_second_cycle() -> None:
    now = datetime(2026, 6, 11, 8, 30, tzinfo=UTC)
    news = [
        MacroNewsItem(
            title="Iran and Israel peace talks collapse after missile strike near Hormuz",
            source="test",
            url="https://example.com/a",
            published_at=now - timedelta(minutes=5),
        )
    ]
    # First cycle: high market stress → EMA initialized to raw score
    db = FakeDb(
        settings={"risk": _risk_settings()},
        markets=[_market()],
        candles=_candles(now),
    )
    monitor = MacroRiskMonitor(Settings(), news_client=StaticNewsClient(news))
    result1 = monitor.run(db, now=now)  # type: ignore[arg-type]
    raw_ema_after_first = result1["state"]["market_stress_ema"]
    assert raw_ema_after_first is not None

    # Second cycle: market stress drops to 0; EMA is smoothed (not a hard reset)
    db2 = FakeDb(
        settings={
            "risk": _risk_settings(),
            "macro_risk_state": result1["state"],
        },
        markets=[_market()],
        candles=[],  # no candles → raw_market_score = 0
    )
    result2 = monitor.run(db2, now=now + timedelta(minutes=1))  # type: ignore[arg-type]
    ema_after_drop = result2["state"]["market_stress_ema"]
    assert ema_after_drop is not None
    # EMA must be strictly between 0 and the previous value (smoothed, not zeroed)
    assert 0 < ema_after_drop < raw_ema_after_first
    # smoothed score stored in state must equal rounded EMA
    assert result2["state"]["market_stress_score"] == round(ema_after_drop)
