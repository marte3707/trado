from __future__ import annotations

from typing import Any, Literal

from app.indicators.core import adx as adx_indicator
from app.indicators.core import hurst_exponent, volatility_persistence
from app.strategies.base import CandleInput

TREND_FOLLOWING_STRATEGIES = {"ema_crossover", "macd", "supertrend"}
MEAN_REVERTING_STRATEGIES = {"rsi", "bollinger_bands"}

RegimeLabel = Literal["trending", "ranging", "mixed"]
RegimeMethod = Literal["adx", "hurst", "garch", "hybrid"]


def classify_regime(adx_value: float, *, trending: float, ranging: float) -> RegimeLabel:
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
) -> RegimeLabel:
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


def _classify_by_hurst(value: float, *, trending: float, ranging: float) -> RegimeLabel:
    """Hurst exponent → regime label. Default thresholds:
    H ≥ 0.55 trending (persistente), H ≤ 0.45 ranging (anti-persistente),
    altrimenti mixed (random walk-ish)."""
    if value >= trending:
        return "trending"
    if value <= ranging:
        return "ranging"
    return "mixed"


def _classify_by_garch(value: float, *, cluster_threshold: float) -> RegimeLabel:
    """Volatility persistence (proxy GARCH) → regime label.

    Una vol clusterizzata indica che il mercato è "in moto" (trending o panic),
    mentre vol noise-like indica un mercato calmo/ranging. Soglia di default
    0.2 sull'autocorrelazione lag-1 dei rendimenti al quadrato.

    NB: GARCH non distingue direzione né strutturale-vs-impulsiva, quindi
    questa metrica è più utile come *secondary* nel modo `hybrid` che da sola.
    """
    if value >= cluster_threshold:
        return "trending"
    if value <= cluster_threshold / 2:
        return "ranging"
    return "mixed"


def _consensus(labels: list[RegimeLabel]) -> RegimeLabel:
    """Aggrega più classificazioni: serve un voto a maggioranza per dichiarare
    trending o ranging; in caso di disaccordo rimane mixed (no-op sui pesi)."""
    if not labels:
        return "mixed"
    trending = sum(1 for label in labels if label == "trending")
    ranging = sum(1 for label in labels if label == "ranging")
    if trending > ranging and trending > len(labels) // 2:
        return "trending"
    if ranging > trending and ranging > len(labels) // 2:
        return "ranging"
    return "mixed"


def detect_regime(
    candles: list[CandleInput],
    *,
    method: RegimeMethod = "adx",
    adx_trending: float = 25.0,
    adx_ranging: float = 18.0,
    adx_period: int = 14,
    adx_confirm_candles: int = 1,
    hurst_trending: float = 0.55,
    hurst_ranging: float = 0.45,
    hurst_min_lag: int = 2,
    hurst_max_lag: int = 20,
    garch_window: int = 50,
    garch_lag: int = 1,
    garch_cluster_threshold: float = 0.2,
) -> dict[str, Any] | None:
    """Classifica il regime di mercato usando il metodo selezionato.

    Ritorna None se la serie è troppo corta per il metodo richiesto, altrimenti
    un dict con `regime` (label finale) e le metriche/sub-regimes di supporto.
    In `hybrid` esegue tutti i metodi disponibili e prende il consensus.
    """
    closes = [c.close for c in candles]
    metrics: dict[str, Any] = {"method": method}
    candidates: list[RegimeLabel] = []

    if method in ("adx", "hybrid"):
        if len(candles) >= adx_period * 2 + 1:
            adx_series = adx_indicator(
                [c.high for c in candles],
                [c.low for c in candles],
                closes,
                adx_period,
            )
            latest_adx = adx_series[-1] if adx_series else None
            if latest_adx is not None:
                adx_label = _classify_regime_with_hysteresis(
                    adx_series,
                    trending=adx_trending,
                    ranging=adx_ranging,
                    confirm_candles=max(1, int(adx_confirm_candles)),
                )
                metrics["adx"] = latest_adx
                metrics["adx_regime"] = adx_label
                candidates.append(adx_label)

    if method in ("hurst", "hybrid"):
        h_value = hurst_exponent(
            closes,
            min_lag=hurst_min_lag,
            max_lag=hurst_max_lag,
        )
        if h_value is not None:
            h_label = _classify_by_hurst(h_value, trending=hurst_trending, ranging=hurst_ranging)
            metrics["hurst"] = h_value
            metrics["hurst_regime"] = h_label
            candidates.append(h_label)

    if method in ("garch", "hybrid"):
        v_value = volatility_persistence(closes, lag=garch_lag, window=garch_window)
        if v_value is not None:
            g_label = _classify_by_garch(v_value, cluster_threshold=garch_cluster_threshold)
            metrics["garch"] = v_value
            metrics["garch_regime"] = g_label
            candidates.append(g_label)

    if not candidates:
        return None

    if method == "hybrid":
        final_label: RegimeLabel = _consensus(candidates)
    else:
        # in modalità single-method il candidato è uno solo; in hybrid ce ne sono fino a 3.
        final_label = candidates[0]
    metrics["regime"] = final_label
    return metrics


def apply_regime_filter(
    weights: dict[str, int],
    candles: list[CandleInput],
    *,
    enabled: bool,
    method: RegimeMethod = "adx",
    adx_trending: float = 25.0,
    adx_ranging: float = 18.0,
    adx_period: int = 14,
    confirm_candles: int = 1,
    off_regime_weight_multiplier: float = 0.0,
    hurst_trending: float = 0.55,
    hurst_ranging: float = 0.45,
    hurst_min_lag: int = 2,
    hurst_max_lag: int = 20,
    garch_window: int = 50,
    garch_lag: int = 1,
    garch_cluster_threshold: float = 0.2,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    if not enabled:
        return weights, None
    detection = detect_regime(
        candles,
        method=method,
        adx_trending=adx_trending,
        adx_ranging=adx_ranging,
        adx_period=adx_period,
        adx_confirm_candles=confirm_candles,
        hurst_trending=hurst_trending,
        hurst_ranging=hurst_ranging,
        hurst_min_lag=hurst_min_lag,
        hurst_max_lag=hurst_max_lag,
        garch_window=garch_window,
        garch_lag=garch_lag,
        garch_cluster_threshold=garch_cluster_threshold,
    )
    if detection is None:
        return weights, None
    regime: RegimeLabel = detection["regime"]
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
    info: dict[str, Any] = {
        **detection,
        "neutralized": neutralized,
        "confirm_candles": confirm_candles,
        "off_regime_weight_multiplier": multiplier,
    }
    return filtered, info
