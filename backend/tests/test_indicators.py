from app.indicators.core import (
    adx,
    atr,
    bollinger_bands,
    ema,
    fibonacci_levels,
    latest_candlestick_pattern,
    macd,
    rsi,
    supertrend,
    support_resistance_levels,
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
