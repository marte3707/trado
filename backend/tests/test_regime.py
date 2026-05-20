from datetime import datetime, timezone

from app.engine.regime import apply_regime_filter, classify_regime
from app.strategies.base import CandleInput


def _trending_candles(count: int = 60) -> list[CandleInput]:
    candles: list[CandleInput] = []
    for i in range(count):
        close = 100.0 + i * 0.5
        candles.append(
            CandleInput(
                datetime.now(timezone.utc),
                close - 0.1,
                close + 0.3,
                close - 0.3,
                close,
                1000,
            )
        )
    return candles


def _choppy_candles(count: int = 60) -> list[CandleInput]:
    candles: list[CandleInput] = []
    for i in range(count):
        close = 100.0 + ((-1) ** i) * 0.2
        candles.append(
            CandleInput(
                datetime.now(timezone.utc),
                close - 0.1,
                close + 0.4,
                close - 0.4,
                close,
                1000,
            )
        )
    return candles


def test_classify_regime_thresholds() -> None:
    assert classify_regime(30.0, trending=25.0, ranging=18.0) == "trending"
    assert classify_regime(15.0, trending=25.0, ranging=18.0) == "ranging"
    assert classify_regime(22.0, trending=25.0, ranging=18.0) == "mixed"


def test_apply_regime_filter_disabled_returns_weights_unchanged() -> None:
    weights = {"rsi": 9, "ema_crossover": 14}
    candles = _trending_candles()

    result_weights, regime_info = apply_regime_filter(weights, candles, enabled=False)

    assert result_weights == weights
    assert regime_info is None


def test_apply_regime_filter_neutralizes_mean_reverting_in_trending_market() -> None:
    weights = {"rsi": 9, "ema_crossover": 14, "bollinger_bands": 8, "macd": 12}
    candles = _trending_candles()

    result_weights, regime_info = apply_regime_filter(weights, candles, enabled=True)

    assert regime_info is not None
    assert regime_info["regime"] == "trending"
    assert result_weights["rsi"] == 0
    assert result_weights["bollinger_bands"] == 0
    assert result_weights["ema_crossover"] == 14
    assert result_weights["macd"] == 12
    assert set(regime_info["neutralized"]) == {"rsi", "bollinger_bands"}


def test_apply_regime_filter_neutralizes_trend_following_in_ranging_market() -> None:
    weights = {"rsi": 9, "ema_crossover": 14, "supertrend": 15}
    candles = _choppy_candles()

    result_weights, regime_info = apply_regime_filter(weights, candles, enabled=True)

    assert regime_info is not None
    # mercato choppy → ranging
    assert regime_info["regime"] in {"ranging", "mixed"}
    if regime_info["regime"] == "ranging":
        assert result_weights["ema_crossover"] == 0
        assert result_weights["supertrend"] == 0
        assert result_weights["rsi"] == 9


def test_apply_regime_filter_soft_multiplier_scales_instead_of_zeroing() -> None:
    """F-14: con `off_regime_weight_multiplier=0.5` le strategie out-of-regime
    vengono dimezzate invece di azzerate (default rimane 0.0 = full neutralize)."""
    weights = {"rsi": 9, "ema_crossover": 14, "bollinger_bands": 8, "macd": 12}
    candles = _trending_candles()

    result_weights, regime_info = apply_regime_filter(
        weights, candles, enabled=True, off_regime_weight_multiplier=0.5
    )

    assert regime_info is not None
    assert regime_info["regime"] == "trending"
    # Mean-reverters scalati a metà, non azzerati (banker's rounding: 4.5→4)
    assert result_weights["rsi"] == 4  # round(9 * 0.5) con banker's rounding
    assert result_weights["bollinger_bands"] == 4  # round(8 * 0.5)
    # Trend-followers invariati
    assert result_weights["ema_crossover"] == 14
    assert result_weights["macd"] == 12


def test_apply_regime_filter_too_short_history_no_op() -> None:
    weights = {"rsi": 9}
    candles = _trending_candles(count=10)

    result_weights, regime_info = apply_regime_filter(weights, candles, enabled=True)

    assert result_weights == weights
    assert regime_info is None
