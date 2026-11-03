from datetime import UTC, datetime

from app.core.config import Settings
from app.db.models import AppSetting, Market, Position
from app.services.hyperliquid_ws import (
    CandleSubscription,
    ClosedCandleAccumulator,
    L2BookSubscription,
    build_candle_subscription_plan,
    build_l2_book_subscription_plan,
    candle_subscription_key_from_ack,
    market_data_subscription_fingerprint,
)


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self.rows)


class FakeDb:
    def __init__(self, markets: list[Market], positions: list[Position]) -> None:
        self.markets = markets
        self.positions = positions
        self.settings = {
            "timeframes": AppSetting(
                key="timeframes",
                value={"enabled": ["15m", "1h"], "monitoring_only": ["5m"]},
            )
        }

    def query(self, model):
        if model is Market:
            return FakeQuery(self.markets)
        if model is Position:
            return FakeQuery(self.positions)
        raise AssertionError(f"unexpected query model: {model}")

    def get(self, model, key):
        if model is AppSetting:
            return self.settings.get(key)
        return None


def _market(market_id: int, symbol: str) -> Market:
    return Market(
        id=market_id,
        exchange="hyperliquid",
        symbol=symbol,
        base=symbol,
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": symbol},
    )


def test_subscription_plan_covers_enabled_and_restricts_monitoring() -> None:
    btc = _market(1, "BTC")
    eth = _market(2, "ETH")
    sol = _market(3, "SOL")
    db = FakeDb([btc, eth, sol], [])

    plan = build_candle_subscription_plan(db, Settings(), max_subscriptions=950)
    keys = {(item.market_id, item.timeframe) for item in plan}

    assert keys == {
        (1, "5m"),
        (2, "5m"),
        (1, "15m"),
        (2, "15m"),
        (3, "15m"),
        (1, "1h"),
        (2, "1h"),
        (3, "1h"),
    }
    assert (3, "5m") not in keys


def test_subscription_plan_keeps_open_position_timeframe_first() -> None:
    btc = _market(1, "BTC")
    sol = _market(3, "SOL")
    position = Position(
        market_id=3,
        mode="live",
        side="long",
        entry_price=100,
        size=1,
        status="open",
        metadata_json={"source_timeframe": "4h"},
        opened_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    db = FakeDb([btc, sol], [position])

    plan = build_candle_subscription_plan(db, Settings(), max_subscriptions=3)

    assert plan[0] == CandleSubscription(3, "SOL", "4h")
    assert len(plan) == 3


def test_closed_candle_accumulator_emits_previous_once() -> None:
    accumulator = ClosedCandleAccumulator()
    subscription = CandleSubscription(1, "BTC", "5m")
    first = {
        "t": 1_000,
        "T": 300_999,
        "s": "BTC",
        "i": "5m",
        "o": "100",
        "h": "102",
        "l": "99",
        "c": "101",
        "v": "10",
    }
    first_update = {**first, "c": "101.5", "v": "11"}
    second = {**first, "t": 301_000, "T": 600_999, "o": "101.5"}

    assert accumulator.ingest(subscription, first, now_ms=200_000) == []
    assert accumulator.ingest(subscription, first_update, now_ms=250_000) == []
    assert accumulator.ingest(subscription, second, now_ms=400_000) == [first_update]
    assert accumulator.ingest(subscription, second, now_ms=400_000) == []


def test_candle_subscription_payload_matches_hyperliquid_protocol() -> None:
    subscription = CandleSubscription(1, "BTC", "15m")

    assert subscription.payload() == {
        "method": "subscribe",
        "subscription": {"type": "candle", "coin": "BTC", "interval": "15m"},
    }


def test_l2_subscription_uses_remaining_websocket_capacity() -> None:
    candles = [
        CandleSubscription(1, "BTC", "15m"),
        CandleSubscription(1, "BTC", "1h"),
        CandleSubscription(2, "ETH", "15m"),
    ]

    assert build_l2_book_subscription_plan(candles, max_subscriptions=5) == [
        L2BookSubscription(1, "BTC"),
        L2BookSubscription(2, "ETH"),
    ]
    assert L2BookSubscription(1, "BTC").payload() == {
        "method": "subscribe",
        "subscription": {"type": "l2Book", "coin": "BTC"},
    }


def test_subscription_fingerprint_includes_l2_on_periodic_refresh() -> None:
    candles = [
        CandleSubscription(1, "BTC", "15m"),
        CandleSubscription(2, "ETH", "15m"),
    ]

    initial = market_data_subscription_fingerprint(
        candles,
        max_subscriptions=4,
    )
    refreshed = market_data_subscription_fingerprint(
        list(candles),
        max_subscriptions=4,
    )

    assert initial == refreshed
    assert initial == (
        CandleSubscription(1, "BTC", "15m"),
        CandleSubscription(2, "ETH", "15m"),
        L2BookSubscription(1, "BTC"),
        L2BookSubscription(2, "ETH"),
    )


def test_subscription_ack_matches_hyperliquid_protocol() -> None:
    message = {
        "channel": "subscriptionResponse",
        "data": {
            "method": "subscribe",
            "subscription": {"type": "candle", "coin": "BTC", "interval": "15m"},
        },
    }

    assert candle_subscription_key_from_ack(message) == ("BTC", "15m")
