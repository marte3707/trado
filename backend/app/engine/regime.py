from __future__ import annotations

from typing import Any

from app.indicators.core import adx as adx_indicator
from app.strategies.base import CandleInput

TREND_FOLLOWING_STRATEGIES = {"ema_crossover", "macd", "supertrend"}
MEAN_REVERTING_STRATEGIES = {"rsi", "bollinger_bands"}


def classify_regime(adx_value: float, *, trending: float, ranging: float) -> str:
    if adx_value >= trending:
        return "trending"
    if adx_value <= ranging:
        return "ranging"
    return "mixed"


def apply_regime_filter(
    weights: dict[str, int],
    candles: list[CandleInput],
    *,
    enabled: bool,
    adx_trending: float = 25.0,
    adx_ranging: float = 18.0,
    adx_period: int = 14,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    if not enabled or len(candles) < adx_period * 2 + 1:
        return weights, None
    adx_series = adx_indicator(
        [candle.high for candle in candles],
        [candle.low for candle in candles],
        [candle.close for candle in candles],
        adx_period,
    )
    latest = adx_series[-1] if adx_series else None
    if latest is None:
        return weights, None
    regime = classify_regime(latest, trending=adx_trending, ranging=adx_ranging)
    filtered = dict(weights)
    neutralized: list[str] = []
    for name in list(filtered.keys()):
        if regime == "ranging" and name in TREND_FOLLOWING_STRATEGIES:
            filtered[name] = 0
            neutralized.append(name)
        elif regime == "trending" and name in MEAN_REVERTING_STRATEGIES:
            filtered[name] = 0
            neutralized.append(name)
    return filtered, {
        "adx": latest,
        "regime": regime,
        "neutralized": neutralized,
    }
