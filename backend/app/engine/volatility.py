from __future__ import annotations

from typing import Any

from app.indicators.core import atr as atr_indicator
from app.strategies.base import CandleInput


def atr_percentile_rank(values: list[float | None]) -> float | None:
    series = [value for value in values if value is not None]
    if len(series) < 20:
        return None
    current = series[-1]
    below_or_equal = sum(1 for value in series if value <= current)
    return below_or_equal / len(series)


def evaluate_volatility(
    candles: list[CandleInput],
    *,
    enabled: bool,
    pct_min: float = 0.25,
    pct_max: float = 0.90,
    atr_period: int = 14,
    min_history: int = 50,
) -> tuple[bool, dict[str, Any] | None]:
    if not enabled or len(candles) < max(min_history, atr_period * 2):
        return True, None
    atr_series = atr_indicator(
        [candle.high for candle in candles],
        [candle.low for candle in candles],
        [candle.close for candle in candles],
        atr_period,
    )
    rank = atr_percentile_rank(atr_series)
    if rank is None:
        return True, None
    passed = pct_min <= rank <= pct_max
    info = {
        "atr_percentile_rank": rank,
        "pct_min": pct_min,
        "pct_max": pct_max,
        "passed": passed,
    }
    return passed, info
