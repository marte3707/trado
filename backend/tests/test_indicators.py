from app.indicators.core import (
    adx,
    atr,
    bollinger_bands,
    ema,
    fibonacci_levels,
    hurst_exponent,
    latest_candlestick_pattern,
    macd,
    rsi,
    supertrend,
    support_resistance_levels,
    volatility_persistence,
    vwap,
)


def test_ema_returns_latest_value_for_sufficient_data() -> None:
    values = [float(i) for i in range(1, 31)]
    result = ema(values, 10)
    assert result[-1] is not None
    assert result[-1] > result[-2]


def test_rsi_bounds() -> None:
    values = [1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12]
    result = rsi(values, 14)
    assert result[-1] is not None
    assert 0 <= result[-1] <= 100


def test_macd_shapes_match_input() -> None:
    values = [float(i) for i in range(1, 80)]
    result = macd(values)
    assert len(result["macd"]) == len(values)
    assert len(result["signal"]) == len(values)
    assert len(result["histogram"]) == len(values)


def test_atr_positive() -> None:
    highs = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
    lows = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    result = atr(highs, lows, closes, 14)
    assert result[-1] is not None
    assert result[-1] > 0


def test_bollinger_bands_shape_and_bounds() -> None:
    values = [float(i) for i in range(1, 40)]
    result = bollinger_bands(values, 20, 2)
    assert len(result["upper"]) == len(values)
    assert result["upper"][-1] is not None
    assert result["lower"][-1] is not None
    assert result["upper"][-1] > result["lower"][-1]


def test_vwap_returns_rolling_value() -> None:
    highs = [11, 12, 13, 14, 15]
    lows = [9, 10, 11, 12, 13]
    closes = [10, 11, 12, 13, 14]
    volumes = [1, 2, 3, 4, 5]
    result = vwap(highs, lows, closes, volumes, period=3)
    assert result[-1] is not None
    assert lows[-1] <= result[-1] <= highs[-1]


def test_supertrend_returns_direction() -> None:
    highs = [float(i + 2) for i in range(1, 40)]
    lows = [float(i) for i in range(1, 40)]
    closes = [float(i + 1) for i in range(1, 40)]
    result = supertrend(highs, lows, closes, 10, 3)
    assert result["direction"][-1] in {"up", "down"}


def test_fibonacci_levels_detect_swing() -> None:
    highs = [10, 11, 12, 15, 14]
    lows = [9, 10, 10.5, 11, 12]
    result = fibonacci_levels(highs, lows, lookback=5)
    assert result["swing_high"] == 15
    assert result["swing_low"] == 9
    assert result["levels"]


def test_support_resistance_levels_include_extremes() -> None:
    highs = [10, 12, 11, 13, 12, 14, 13]
    lows = [8, 9, 8.5, 10, 9.5, 11, 10.5]
    result = support_resistance_levels(highs, lows, lookback=7, pivot_window=1)
    assert result["supports"]
    assert result["resistances"]


def test_latest_candlestick_pattern_detects_engulfing() -> None:
    result = latest_candlestick_pattern(
        opens=[10, 9],
        highs=[10.5, 11],
        lows=[8.5, 8.7],
        closes=[9, 10.8],
    )
    assert result["pattern"] == "bullish_engulfing"
    assert result["direction"] == "LONG"


def test_adx_returns_none_when_history_is_too_short() -> None:
    highs = [10.0 + i for i in range(10)]
    lows = [9.0 + i for i in range(10)]
    closes = [9.5 + i for i in range(10)]

    result = adx(highs, lows, closes, 14)

    assert all(value is None for value in result)


def test_adx_high_in_trending_market() -> None:
    closes = [100.0 + i * 0.5 for i in range(80)]
    highs = [close + 0.3 for close in closes]
    lows = [close - 0.3 for close in closes]

    result = adx(highs, lows, closes, 14)
    last = result[-1]

    assert last is not None
    assert last > 25


def test_adx_low_in_choppy_market() -> None:
    closes = [100.0 + ((-1) ** i) * 0.2 for i in range(80)]
    highs = [close + 0.4 for close in closes]
    lows = [close - 0.4 for close in closes]

    result = adx(highs, lows, closes, 14)
    last = result[-1]

    assert last is not None
    assert last < 30


# ---------------------------------------------------------------------------
# Robustness tests (added during §2.1 perf investigation — vedi doc per dettagli)
# ---------------------------------------------------------------------------

import math


def test_atr_finite_for_diverse_series() -> None:
    highs = [100 + i * 0.5 + math.sin(i / 4) * 2 for i in range(100)]
    lows = [h - 1.0 - abs(math.cos(i / 5)) for i, h in enumerate(highs)]
    closes = [(h + l) / 2 + math.sin(i / 6) * 0.3 for i, (h, l) in enumerate(zip(highs, lows))]

    result = atr(highs, lows, closes, 14)
    # I primi 13 sono None (warmup EMA), gli altri devono essere float positivi
    assert all(v is None for v in result[:13])
    assert all(v is not None and v > 0 for v in result[13:])


def test_rsi_bounded_for_long_series() -> None:
    values = [100.0 + math.sin(i / 5) * 10 + (i % 11) for i in range(200)]
    result = rsi(values, 14)

    valid = [v for v in result if v is not None]
    assert len(valid) >= 180
    for v in valid:
        assert 0 <= v <= 100


def test_adx_monotonic_in_trending_series() -> None:
    # Trend forte: ADX dovrebbe crescere o restare alto
    closes = [100.0 + i * 0.4 for i in range(100)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    result = adx(highs, lows, closes, 14)
    final = result[-1]
    assert final is not None
    assert final > 40


def test_hurst_exponent_persistent_returns_above_half() -> None:
    # AR(1) sui log-returns con phi=0.7 → returns persistenti, H > 0.5
    import math
    import random

    random.seed(42)
    returns = [0.0]
    for _ in range(300):
        returns.append(0.7 * returns[-1] + random.gauss(0, 0.005))
    closes = [100.0]
    for r in returns[1:]:
        closes.append(closes[-1] * math.exp(r))
    h = hurst_exponent(closes, min_lag=2, max_lag=20)
    assert h is not None
    assert h > 0.6


def test_hurst_exponent_mean_reverting_series_below_half() -> None:
    # Oscillazione ad alta frequenza attorno a 100 → anti-persistente, H < 0.5
    closes = [100.0 + ((-1) ** i) * 0.5 + (i % 3) * 0.1 for i in range(200)]
    h = hurst_exponent(closes, min_lag=2, max_lag=20)
    assert h is not None
    assert h < 0.5


def test_hurst_exponent_short_series_returns_none() -> None:
    closes = [100.0 + i for i in range(20)]
    assert hurst_exponent(closes, min_lag=2, max_lag=20) is None


def test_hurst_exponent_non_positive_returns_none() -> None:
    closes = [100.0] * 50 + [0.0]
    assert hurst_exponent(closes, min_lag=2, max_lag=10) is None


def test_volatility_persistence_constant_series_returns_none() -> None:
    # Rendimenti tutti zero → varianza nulla → degenere
    closes = [100.0] * 100
    assert volatility_persistence(closes, window=50) is None


def test_volatility_persistence_clustered_volatility_positive_autocorr() -> None:
    # Costruzione di una serie con vol clustering esplicito: blocchi alternati
    # di alta e bassa volatilità → autocorrelazione su squared returns positiva
    closes = [100.0]
    high_block = [0.04, -0.04, 0.04, -0.04, 0.04, -0.04, 0.04, -0.04, 0.04, -0.04]
    low_block = [0.001, -0.001, 0.001, -0.001, 0.001, -0.001, 0.001, -0.001, 0.001, -0.001]
    for _ in range(6):
        for r in high_block + low_block:
            closes.append(closes[-1] * (1 + r))
    result = volatility_persistence(closes, lag=1, window=80)
    assert result is not None
    # I blocchi mantengono σ² persistente al loro interno → autocorr lag-1 > 0
    assert result > 0.1


def test_volatility_persistence_short_series_returns_none() -> None:
    closes = [100.0 + i * 0.1 for i in range(30)]
    assert volatility_persistence(closes, window=50) is None
