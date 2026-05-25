from __future__ import annotations

from collections.abc import Sequence
from math import log, sqrt
from typing import cast, Any


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    multiplier = 2 / (period + 1)
    previous = sma
    for index in range(period, len(values)):
        previous = (values[index] - previous) * multiplier + previous
        result[index] = previous
    return result


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    if len(values) <= period:
        return [None] * len(values)
    result: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0)
        loss = abs(min(change, 0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return result


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, list[float | None]]:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line: list[float | None] = []
    compact: list[float] = []
    for fast_value, slow_value in zip(fast_ema, slow_ema, strict=False):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
            continue
        macd_value = fast_value - slow_value
        macd_line.append(macd_value)
        compact.append(macd_value)
    signal_compact = ema(compact, signal)
    signal_line: list[float | None] = [None] * len(values)
    compact_index = 0
    for index, value in enumerate(macd_line):
        if value is None:
            continue
        signal_line[index] = signal_compact[compact_index]
        compact_index += 1
    histogram = [
        None if line is None or sig is None else line - sig
        for line, sig in zip(macd_line, signal_line, strict=False)
    ]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float | None]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must have same length")
    true_ranges: list[float] = []
    for index in range(len(closes)):
        if index == 0:
            true_ranges.append(highs[index] - lows[index])
            continue
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return ema(true_ranges, period)


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must have same length")
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    result: list[float | None] = [None] * n
    if n < period * 2 + 1:
        return result

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    true_range = [0.0] * n
    for index in range(1, n):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
        true_range[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )

    smoothed_plus_dm: list[float | None] = [None] * n
    smoothed_minus_dm: list[float | None] = [None] * n
    smoothed_tr: list[float | None] = [None] * n
    smoothed_plus_dm[period] = sum(plus_dm[1 : period + 1])
    smoothed_minus_dm[period] = sum(minus_dm[1 : period + 1])
    smoothed_tr[period] = sum(true_range[1 : period + 1])
    for index in range(period + 1, n):
        prev_p = cast(float, smoothed_plus_dm[index - 1])
        prev_m = cast(float, smoothed_minus_dm[index - 1])
        prev_t = cast(float, smoothed_tr[index - 1])
        smoothed_plus_dm[index] = prev_p - (prev_p / period) + plus_dm[index]
        smoothed_minus_dm[index] = prev_m - (prev_m / period) + minus_dm[index]
        smoothed_tr[index] = prev_t - (prev_t / period) + true_range[index]

    dx: list[float | None] = [None] * n
    for index in range(period, n):
        tr_value = smoothed_tr[index]
        if tr_value is None or tr_value == 0:
            continue
        plus_di = 100 * cast(float, smoothed_plus_dm[index]) / tr_value
        minus_di = 100 * cast(float, smoothed_minus_dm[index]) / tr_value
        denominator = plus_di + minus_di
        dx[index] = 0.0 if denominator == 0 else 100 * abs(plus_di - minus_di) / denominator

    first_adx_index = period * 2
    if first_adx_index >= n:
        return result
    window = [value for value in dx[period + 1 : first_adx_index + 1] if value is not None]
    if len(window) < period:
        return result
    result[first_adx_index] = sum(window) / period
    for index in range(first_adx_index + 1, n):
        prev = result[index - 1]
        current_dx = dx[index]
        if prev is None or current_dx is None:
            continue
        result[index] = (prev * (period - 1) + current_dx) / period
    return result


def simple_moving_average(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be positive")
    for index in range(period - 1, len(values)):
        result[index] = sum(values[index - period + 1 : index + 1]) / period
    return result


def bollinger_bands(
    values: Sequence[float], period: int = 20, stddev: float = 2.0
) -> dict[str, list[float | None]]:
    if period <= 1:
        raise ValueError("period must be greater than 1")
    middle = simple_moving_average(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    bandwidth: list[float | None] = [None] * len(values)
    percent_b: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        mean = middle[index]
        if mean is None:
            continue
        variance = sum((value - mean) ** 2 for value in window) / period
        deviation = sqrt(variance)
        upper_value = mean + stddev * deviation
        lower_value = mean - stddev * deviation
        upper[index] = upper_value
        lower[index] = lower_value
        bandwidth[index] = (upper_value - lower_value) / mean if mean else None
        percent_b[index] = (
            (values[index] - lower_value) / (upper_value - lower_value)
            if upper_value != lower_value
            else None
        )
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,
        "percent_b": percent_b,
    }


def vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    period: int | None = None,
) -> list[float | None]:
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("highs, lows, closes, volumes must have same length")
    if period is not None and period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(closes)
    typical_volume: list[float] = []
    for high, low, close, volume in zip(highs, lows, closes, volumes, strict=False):
        typical_volume.append(((high + low + close) / 3) * volume)
    for index in range(len(closes)):
        start = 0 if period is None else max(0, index - period + 1)
        volume_sum = sum(volumes[start : index + 1])
        if volume_sum == 0:
            continue
        if period is not None and index - start + 1 < period:
            continue
        result[index] = sum(typical_volume[start : index + 1]) / volume_sum
    return result


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, list[float | None] | list[str | None]]:
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must have same length")
    if period <= 0:
        raise ValueError("period must be positive")
    atr_values = atr(highs, lows, closes, period)
    upper_band: list[float | None] = [None] * len(closes)
    lower_band: list[float | None] = [None] * len(closes)
    trend: list[float | None] = [None] * len(closes)
    direction: list[str | None] = [None] * len(closes)
    for index in range(len(closes)):
        current_atr = atr_values[index]
        if current_atr is None:
            continue
        hl2 = (highs[index] + lows[index]) / 2
        basic_upper = hl2 + multiplier * current_atr
        basic_lower = hl2 - multiplier * current_atr
        if index == 0 or upper_band[index - 1] is None or lower_band[index - 1] is None:
            upper_band[index] = basic_upper
            lower_band[index] = basic_lower
            trend[index] = basic_lower
            direction[index] = "up"
            continue
        previous_upper = cast(float, upper_band[index - 1])
        previous_lower = cast(float, lower_band[index - 1])
        previous_close = closes[index - 1]
        upper_band[index] = (
            basic_upper
            if basic_upper < previous_upper or previous_close > previous_upper
            else previous_upper
        )
        lower_band[index] = (
            basic_lower
            if basic_lower > previous_lower or previous_close < previous_lower
            else previous_lower
        )
        previous_direction = direction[index - 1]
        if previous_direction == "down":
            if closes[index] > cast(float, upper_band[index]):
                direction[index] = "up"
                trend[index] = lower_band[index]
            else:
                direction[index] = "down"
                trend[index] = upper_band[index]
        else:
            if closes[index] < cast(float, lower_band[index]):
                direction[index] = "down"
                trend[index] = upper_band[index]
            else:
                direction[index] = "up"
                trend[index] = lower_band[index]
    return {"trend": trend, "direction": direction, "upper": upper_band, "lower": lower_band}


def fibonacci_levels(
    highs: Sequence[float], lows: Sequence[float], lookback: int = 120
) -> dict[str, Any]:
    if not highs or not lows:
        return {"trend": None, "swing_high": None, "swing_low": None, "levels": {}}
    if len(highs) != len(lows):
        raise ValueError("highs, lows must have same length")
    window_start = max(0, len(highs) - lookback)
    high_window = highs[window_start:]
    low_window = lows[window_start:]
    swing_high = max(high_window)
    swing_low = min(low_window)
    swing_high_index = window_start + high_window.index(swing_high)
    swing_low_index = window_start + low_window.index(swing_low)
    diff = swing_high - swing_low
    if diff == 0:
        return {"trend": "flat", "swing_high": swing_high, "swing_low": swing_low, "levels": {}}
    ratios = (0.236, 0.382, 0.5, 0.618, 0.786)
    if swing_low_index < swing_high_index:
        trend = "up"
        levels = {f"{ratio:.3f}": swing_high - diff * ratio for ratio in ratios}
        extension = swing_high + diff * 0.272
    else:
        trend = "down"
        levels = {f"{ratio:.3f}": swing_low + diff * ratio for ratio in ratios}
        extension = swing_low - diff * 0.272
    return {
        "trend": trend,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "extension_1_272": extension,
        "levels": levels,
    }


def support_resistance_levels(
    highs: Sequence[float],
    lows: Sequence[float],
    lookback: int = 120,
    pivot_window: int = 3,
    tolerance_pct: float = 0.0025,
) -> dict[str, list[float]]:
    if len(highs) != len(lows):
        raise ValueError("highs, lows must have same length")
    if pivot_window <= 0:
        raise ValueError("pivot_window must be positive")
    start = max(0, len(highs) - lookback)
    supports: list[float] = []
    resistances: list[float] = []
    for index in range(max(pivot_window, start), len(highs) - pivot_window):
        low_window = lows[index - pivot_window : index + pivot_window + 1]
        high_window = highs[index - pivot_window : index + pivot_window + 1]
        if lows[index] == min(low_window):
            supports.append(lows[index])
        if highs[index] == max(high_window):
            resistances.append(highs[index])
    if lows[start:]:
        supports.append(min(lows[start:]))
    if highs[start:]:
        resistances.append(max(highs[start:]))
    return {
        "supports": _cluster_levels(supports, tolerance_pct),
        "resistances": _cluster_levels(resistances, tolerance_pct),
    }


def latest_candlestick_pattern(
    opens: Sequence[float], highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> dict[str, str | int | float | None]:
    if not (len(opens) == len(highs) == len(lows) == len(closes)):
        raise ValueError("opens, highs, lows, closes must have same length")
    if len(closes) < 2:
        return {"pattern": None, "direction": "NEUTRAL", "confidence": 0}
    index = len(closes) - 1
    body = abs(closes[index] - opens[index])
    candle_range = highs[index] - lows[index]
    upper_wick = highs[index] - max(opens[index], closes[index])
    lower_wick = min(opens[index], closes[index]) - lows[index]
    previous_bearish = closes[index - 1] < opens[index - 1]
    previous_bullish = closes[index - 1] > opens[index - 1]
    current_bullish = closes[index] > opens[index]
    current_bearish = closes[index] < opens[index]
    if (
        previous_bearish
        and current_bullish
        and opens[index] <= closes[index - 1]
        and closes[index] >= opens[index - 1]
    ):
        return {"pattern": "bullish_engulfing", "direction": "LONG", "confidence": 72}
    if (
        previous_bullish
        and current_bearish
        and opens[index] >= closes[index - 1]
        and closes[index] <= opens[index - 1]
    ):
        return {"pattern": "bearish_engulfing", "direction": "SHORT", "confidence": 72}
    if candle_range > 0 and lower_wick >= body * 2 and upper_wick <= body and current_bullish:
        return {"pattern": "hammer", "direction": "LONG", "confidence": 62}
    if candle_range > 0 and upper_wick >= body * 2 and lower_wick <= body and current_bearish:
        return {"pattern": "shooting_star", "direction": "SHORT", "confidence": 62}
    if len(closes) >= 3:
        first = index - 2
        first_body = abs(closes[first] - opens[first])
        middle_body = abs(closes[index - 1] - opens[index - 1])
        if (
            closes[first] < opens[first]
            and middle_body < first_body * 0.6
            and current_bullish
            and closes[index] > (opens[first] + closes[first]) / 2
        ):
            return {"pattern": "morning_star", "direction": "LONG", "confidence": 78}
        if (
            closes[first] > opens[first]
            and middle_body < first_body * 0.6
            and current_bearish
            and closes[index] < (opens[first] + closes[first]) / 2
        ):
            return {"pattern": "evening_star", "direction": "SHORT", "confidence": 78}
    return {"pattern": None, "direction": "NEUTRAL", "confidence": 0}


def wyckoff_spring_upthrust(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    opens: Sequence[float],
    volumes: Sequence[float],
    lookback: int = 50,
    pivot_window: int = 5,
    tolerance_pct: float = 0.002,
    volume_factor: float = 1.3,
) -> dict[str, Any]:
    """Rileva Spring (falso breakdown sotto supporto) e Upthrust (falso breakout sopra resistenza)."""
    n = len(closes)
    if n < lookback + pivot_window + 2:
        return {"pattern": None, "direction": "NEUTRAL", "confidence": 0,
                "support_level": None, "resistance_level": None}

    end = n - 1
    start = max(0, end - lookback)
    pw = min(pivot_window, max(1, (end - start) // 4))

    range_highs = list(highs[start:end])
    range_lows = list(lows[start:end])
    range_volumes = list(volumes[start:end])

    avg_volume = sum(range_volumes) / len(range_volumes) if range_volumes else 0.0

    pivot_lows: list[float] = []
    pivot_highs: list[float] = []
    for i in range(pw, len(range_lows) - pw):
        if range_lows[i] == min(range_lows[i - pw: i + pw + 1]):
            pivot_lows.append(range_lows[i])
        if range_highs[i] == max(range_highs[i - pw: i + pw + 1]):
            pivot_highs.append(range_highs[i])
    if range_lows:
        pivot_lows.append(min(range_lows))
    if range_highs:
        pivot_highs.append(max(range_highs))

    current_close = closes[end]
    current_high = highs[end]
    current_low = lows[end]
    current_open = opens[end]
    current_volume = volumes[end]

    supports_below = [pl for pl in pivot_lows if pl < current_close]
    support: float | None = max(supports_below) if supports_below else None

    resistances_above = [ph for ph in pivot_highs if ph > current_close]
    resistance: float | None = min(resistances_above) if resistances_above else None

    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
    volume_confirms = volume_ratio >= volume_factor

    if support is not None:
        min_penetration = support * tolerance_pct
        if current_low < support - min_penetration and current_close > support:
            confidence = 65
            if volume_confirms:
                confidence += 10
            if current_close > current_open:
                confidence += 5
            return {
                "pattern": "spring",
                "direction": "LONG",
                "confidence": min(confidence, 85),
                "support_level": round(support, 6),
                "resistance_level": round(resistance, 6) if resistance is not None else None,
                "penetration_pct": round((support - current_low) / support * 100, 3),
                "volume_ratio": round(volume_ratio, 2),
            }

    if resistance is not None:
        min_penetration = resistance * tolerance_pct
        if current_high > resistance + min_penetration and current_close < resistance:
            confidence = 65
            if volume_confirms:
                confidence += 10
            if current_close < current_open:
                confidence += 5
            return {
                "pattern": "upthrust",
                "direction": "SHORT",
                "confidence": min(confidence, 85),
                "support_level": round(support, 6) if support is not None else None,
                "resistance_level": round(resistance, 6),
                "penetration_pct": round((current_high - resistance) / resistance * 100, 3),
                "volume_ratio": round(volume_ratio, 2),
            }

    return {
        "pattern": None,
        "direction": "NEUTRAL",
        "confidence": 0,
        "support_level": round(support, 6) if support is not None else None,
        "resistance_level": round(resistance, 6) if resistance is not None else None,
        "volume_ratio": round(volume_ratio, 2),
    }


def hurst_exponent(
    values: Sequence[float],
    *,
    min_lag: int = 2,
    max_lag: int = 20,
) -> float | None:
    """Stima dell'esponente di Hurst via Rescaled Range (R/S) analysis su log-prezzi.

    H ≈ 0.5 → random walk; H > 0.5 → serie persistente (trending);
    H < 0.5 → serie anti-persistente (mean-reverting).

    Richiede almeno `max_lag * 4` campioni; ritorna None se la serie è troppo
    corta o se i log non sono calcolabili (valori non positivi).
    """
    if min_lag < 2 or max_lag <= min_lag:
        raise ValueError("require 2 <= min_lag < max_lag")
    n = len(values)
    if n < max_lag * 4:
        return None
    if any(v <= 0 for v in values):
        return None
    log_values = [log(v) for v in values]
    returns = [log_values[i] - log_values[i - 1] for i in range(1, n)]
    if not returns:
        return None

    lags: list[int] = []
    rs_values: list[float] = []
    for lag in range(min_lag, max_lag + 1):
        if lag >= len(returns):
            break
        chunks = len(returns) // lag
        if chunks < 1:
            continue
        rs_chunks: list[float] = []
        for chunk_index in range(chunks):
            chunk = returns[chunk_index * lag : (chunk_index + 1) * lag]
            mean = sum(chunk) / lag
            deviations = [r - mean for r in chunk]
            cumulative = []
            running = 0.0
            for d in deviations:
                running += d
                cumulative.append(running)
            r = max(cumulative) - min(cumulative)
            variance = sum(d * d for d in deviations) / lag
            s = sqrt(variance)
            if s <= 1e-12:
                continue
            rs_chunks.append(r / s)
        if not rs_chunks:
            continue
        lags.append(lag)
        rs_values.append(sum(rs_chunks) / len(rs_chunks))

    if len(lags) < 2:
        return None
    # Regressione lineare log-log: H è la pendenza di log(R/S) vs log(lag)
    xs = [log(lag_value) for lag_value in lags]
    ys = [log(rs) for rs in rs_values]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 1e-12:
        return None
    return numerator / denominator


def volatility_persistence(
    values: Sequence[float],
    *,
    lag: int = 1,
    window: int = 50,
) -> float | None:
    """Proxy GARCH-lite: autocorrelazione lag-k dei rendimenti al quadrato.

    Misura il volatility clustering tipico dei modelli GARCH(1,1): valori
    elevati (≳ 0.2) indicano regime di volatilità persistente (cluster), valori
    vicini a zero indicano vol indipendente nel tempo (white-noise sui σ²).

    Usa gli ultimi `window` log-rendimenti. Ritorna None se la serie è troppo
    corta o se la varianza dei rendimenti al quadrato è degenere.
    """
    if lag < 1:
        raise ValueError("lag must be >= 1")
    if window < lag + 10:
        raise ValueError("window must accommodate at least lag + 10 samples")
    n = len(values)
    if n < window + 1:
        return None
    if any(v <= 0 for v in values[-(window + 1) :]):
        return None
    log_values = [log(v) for v in values[-(window + 1) :]]
    returns = [log_values[i] - log_values[i - 1] for i in range(1, len(log_values))]
    squared = [r * r for r in returns]
    if len(squared) <= lag:
        return None
    mean = sum(squared) / len(squared)
    centered = [s - mean for s in squared]
    numerator = sum(centered[i] * centered[i - lag] for i in range(lag, len(centered)))
    denominator = sum(c * c for c in centered)
    if denominator <= 1e-12:
        return None
    return numerator / denominator


def _cluster_levels(levels: Sequence[float], tolerance_pct: float) -> list[float]:
    if not levels:
        return []
    clustered: list[list[float]] = []
    for level in sorted(levels):
        if not clustered:
            clustered.append([level])
            continue
        average = sum(clustered[-1]) / len(clustered[-1])
        tolerance = max(abs(average) * tolerance_pct, 1e-12)
        if abs(level - average) <= tolerance:
            clustered[-1].append(level)
        else:
            clustered.append([level])
    return [sum(group) / len(group) for group in clustered]
