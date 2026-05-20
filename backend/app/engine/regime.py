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


def _classify_regime_with_hysteresis(
    adx_series: list[float | None],
    *,
    trending: float,
    ranging: float,
    confirm_candles: int,
) -> str:
    """Richiede `confirm_candles` consecutivi sopra/sotto le soglie prima di flippare,
    riducendo i flap quando ADX oscilla attorno ai threshold."""
    recent = [v for v in adx_series[-confirm_candles:] if v is not None]
    if len(recent) < confirm_candles:
        # Non abbastanza dati per la conferma — fallback al classify standard sull'ultimo valore
        latest = adx_series[-1] if adx_series else None
        if latest is None:
            return "mixed"
        return classify_regime(latest, trending=trending, ranging=ranging)
    if all(v >= trending for v in recent):
        return "trending"
    if all(v <= ranging for v in recent):
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
    confirm_candles: int = 1,
    off_regime_weight_multiplier: float = 0.0,
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
    regime = _classify_regime_with_hysteresis(
        adx_series,
        trending=adx_trending,
        ranging=adx_ranging,
        confirm_candles=max(1, int(confirm_candles)),
    )
    multiplier = max(0.0, min(1.0, float(off_regime_weight_multiplier)))
    filtered = dict(weights)
    neutralized: list[str] = []
    for name in list(filtered.keys()):
        original = filtered[name]
        if regime == "ranging" and name in TREND_FOLLOWING_STRATEGIES:
            scaled = int(round(original * multiplier))
            filtered[name] = scaled
            if scaled < original:
                neutralized.append(name)
        elif regime == "trending" and name in MEAN_REVERTING_STRATEGIES:
            scaled = int(round(original * multiplier))
            filtered[name] = scaled
            if scaled < original:
                neutralized.append(name)
    return filtered, {
        "adx": latest,
        "regime": regime,
        "neutralized": neutralized,
        "confirm_candles": confirm_candles,
        "off_regime_weight_multiplier": multiplier,
    }
