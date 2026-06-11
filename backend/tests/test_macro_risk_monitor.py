from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import AppSetting, Candle, DecisionLog, Market
from app.engine.macro_risk_monitor import (
    MacroNewsItem,
    MacroRiskMonitor,
    parse_feed,
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


def _risk_settings(**monitor_overrides: Any) -> dict[str, Any]:
    return {
        "min_signal_score": 65,
        "macro_risk_monitor": {
            "enabled": True,
            "actions_enabled": True,
            "news_enabled": True,
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
                    "require_mtf_confluence": True,
                },
                "high": {
                    "size_multiplier": 0.5,
                    "min_score_add": 8,
                    "disable_timeframes": ["5m"],
                    "require_mtf_confluence": True,
                },
                "extreme": {
                    "size_multiplier": 0.25,
                    "min_score_add": 12,
                    "disable_timeframes": ["5m", "15m"],
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
    assert state["policy"]["disable_timeframes"] == ["5m", "15m"]
    assert any(isinstance(row, DecisionLog) for row in db.added)


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
