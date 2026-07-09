from datetime import UTC, datetime, timedelta

from app.core.enums import Direction
from app.db.init_db import DEFAULT_DISABLED_STRATEGIES, DEFAULT_STRATEGIES, DEFAULT_STRATEGY_WEIGHTS
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.registry import strategy_registry
from app.strategies.trend_following_adaptive_strategy import TrendFollowingAdaptiveStrategy

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 10.0,
) -> CandleInput:
    return CandleInput(
        open_time=BASE_TIME + timedelta(minutes=i * 5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _make_context(candles: list[CandleInput]) -> StrategyContext:
    return StrategyContext(
        market="hyperliquid",
        symbol="BTC",
        timeframe="5m",
        market_type="perpetual",
        candles=candles,
    )


def _params() -> dict:
    return {
        "fast_ema": 5,
        "slow_ema": 10,
        "trend_ema": 20,
        "donchian_lookback": 10,
        "adx_period": 5,
        "min_adx": 10.0,
        "atr_period": 5,
        "atr_stop_multiplier": 2.0,
        "reward_r_multiple": 2.0,
        "volume_lookback": 10,
        "min_volume_ratio": 0.5,
        "breakout_atr_buffer": 0.0,
    }


def test_trend_following_adaptive_is_registered_but_disabled_by_default() -> None:
    assert "trend_following_adaptive" in strategy_registry()
    assert "trend_following_adaptive" in DEFAULT_STRATEGIES
    assert "trend_following_adaptive" in DEFAULT_STRATEGY_WEIGHTS
    assert "trend_following_adaptive" in DEFAULT_DISABLED_STRATEGIES


def test_trend_following_adaptive_neutral_when_data_insufficient() -> None:
    candles = [_candle(i, 100, 101, 99, 100, 10) for i in range(8)]
    result = TrendFollowingAdaptiveStrategy().evaluate(_make_context(candles), _params())

    assert result.direction == Direction.NEUTRAL
    assert "insufficient data" in result.reason.lower()


def test_trend_following_adaptive_long_breakout() -> None:
    candles: list[CandleInput] = []
    price = 100.0
    for i in range(40):
        open_price = price
        close_price = price + 0.45
        candles.append(
            _candle(i, open_price, close_price + 0.15, open_price - 0.15, close_price, 12.0)
        )
        price = close_price
    previous_high = max(candle.high for candle in candles[-10:])
    candles.append(_candle(40, price, previous_high + 1.1, price - 0.2, previous_high + 0.9, 18.0))

    result = TrendFollowingAdaptiveStrategy().evaluate(_make_context(candles), _params())

    assert result.direction == Direction.LONG
    assert result.confidence >= 68
    assert "long breakout" in result.reason.lower()
    assert result.suggested_stop_loss is not None
    assert result.suggested_stop_loss < result.suggested_entry
    assert result.suggested_take_profit is not None
    assert result.suggested_take_profit > result.suggested_entry


def test_trend_following_adaptive_short_breakout() -> None:
    candles: list[CandleInput] = []
    price = 140.0
    for i in range(40):
        open_price = price
        close_price = price - 0.45
        candles.append(
            _candle(i, open_price, open_price + 0.15, close_price - 0.15, close_price, 12.0)
        )
        price = close_price
    previous_low = min(candle.low for candle in candles[-10:])
    candles.append(_candle(40, price, price + 0.2, previous_low - 1.1, previous_low - 0.9, 18.0))

    result = TrendFollowingAdaptiveStrategy().evaluate(_make_context(candles), _params())

    assert result.direction == Direction.SHORT
    assert result.confidence >= 68
    assert "short breakout" in result.reason.lower()
    assert result.suggested_stop_loss is not None
    assert result.suggested_stop_loss > result.suggested_entry
    assert result.suggested_take_profit is not None
    assert result.suggested_take_profit < result.suggested_entry


def test_trend_following_adaptive_rejects_breakout_without_ema_alignment() -> None:
    candles: list[CandleInput] = []
    price = 120.0
    for i in range(35):
        open_price = price
        close_price = price - 0.35
        candles.append(
            _candle(i, open_price, open_price + 0.12, close_price - 0.12, close_price, 12.0)
        )
        price = close_price
    previous_high = max(candle.high for candle in candles[-10:])
    candles.append(_candle(35, price, previous_high + 1.0, price - 0.2, previous_high + 0.8, 18.0))

    result = TrendFollowingAdaptiveStrategy().evaluate(_make_context(candles), _params())

    assert result.direction == Direction.NEUTRAL
    assert "ema_stack_not_aligned" in result.reason
