from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.enums import Direction
from app.db.models import Candle, Market
from app.engine.edge_profile import signal_threshold_adjustment
from app.engine.macro_direction import btc_macro_direction, suspended_penalty_family


class FakeQuery:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def filter(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        return self

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self.rows[:count])

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return list(self.rows)


class FakeDb:
    def __init__(self, market: Market | None, candles: list[Candle]) -> None:
        self.market = market
        # btc_macro_direction ordina desc poi inverte: serviamo già in ordine desc.
        self.candles = list(reversed(candles))

    def query(self, model: type[Any], *_args: Any, **_kwargs: Any) -> FakeQuery:
        if model is Market:
            return FakeQuery([self.market] if self.market else [])
        if model is Candle:
            return FakeQuery(self.candles)
        return FakeQuery([])


def _market() -> Market:
    return Market(id=1, symbol="BTC", base="BTC", quote="USDC", market_type="perp", active=True)


def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            market_id=1,
            timeframe="4h",
            open_time=start + timedelta(hours=4 * index),
            open=Decimal(str(close)),
            high=Decimal(str(close + 10)),
            low=Decimal(str(close - 10)),
            close=Decimal(str(close)),
            volume=Decimal("100"),
        )
        for index, close in enumerate(closes)
    ]


_FAST_SETTINGS = {"macro_direction": {"fast_ema_period": 5, "slow_ema_period": 20}}


def test_macro_direction_bullish_when_above_rising_emas() -> None:
    closes = [100.0 + index * 2 for index in range(60)]  # uptrend costante
    db = FakeDb(_market(), _candles(closes))

    result = btc_macro_direction(db, _FAST_SETTINGS)  # type: ignore[arg-type]

    assert result["direction"] == "bullish"


def test_macro_direction_bearish_when_below_falling_emas() -> None:
    closes = [300.0 - index * 2 for index in range(60)]  # downtrend costante
    db = FakeDb(_market(), _candles(closes))

    result = btc_macro_direction(db, _FAST_SETTINGS)  # type: ignore[arg-type]

    assert result["direction"] == "bearish"


def test_macro_direction_neutral_without_btc_market() -> None:
    db = FakeDb(None, [])

    result = btc_macro_direction(db, _FAST_SETTINGS)  # type: ignore[arg-type]

    assert result["direction"] == "neutral"
    assert result["reason"] == "btc_market_not_found"


def test_macro_direction_neutral_with_insufficient_candles() -> None:
    db = FakeDb(_market(), _candles([100.0] * 5))

    result = btc_macro_direction(db, _FAST_SETTINGS)  # type: ignore[arg-type]

    assert result["direction"] == "neutral"
    assert "insufficient_candles" in result["reason"]


def test_suspended_family_long_only_when_bullish() -> None:
    assert suspended_penalty_family({"direction": "bullish"}, {}) == "LONG"
    assert suspended_penalty_family({"direction": "bearish"}, {}) is None
    assert suspended_penalty_family({"direction": "neutral"}, {}) is None


def test_suspension_respects_disable_flag() -> None:
    risk = {"regime_aware_directional_penalties": False}

    assert suspended_penalty_family({"direction": "bullish"}, risk) is None


def test_threshold_adjustment_suspends_long_components_only() -> None:
    risk = {
        "max_combined_score_adjustment": 15,
        "directional_score_adjustments": {"LONG:5m": 8, "SHORT:5m": 4},
        "score_bucket_adjustments": {"LONG:70-79": 5},
        "market_score_adjustments": {"1": 6},
        "directional_min_score_adjustments": {"LONG:5m": 20},
    }

    suspended = signal_threshold_adjustment(
        risk,
        market_id=1,
        timeframe="5m",
        direction=Direction.LONG,
        score=75,
        suspended_family="LONG",
    )
    # Le penalità direzionali LONG sono azzerate; quelle neutre (market) restano.
    assert suspended["components"]["directional"] == 0
    assert suspended["components"]["score_bucket"] == 0
    assert suspended["components"]["directional_min_score"] == 0
    assert suspended["components"]["market"] == 6
    assert suspended["suspended_family"] == "LONG"

    active = signal_threshold_adjustment(
        risk,
        market_id=1,
        timeframe="5m",
        direction=Direction.SHORT,
        score=75,
        suspended_family="LONG",
    )
    # La famiglia SHORT non è sospesa: penalità SHORT pienamente attive.
    assert active["components"]["directional"] == 4
    assert active["suspended_family"] is None
