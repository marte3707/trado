from __future__ import annotations

from typing import Any

from app.core.enums import Direction
from app.indicators.core import atr, ema
from app.strategies.base import CandleInput

HIGHER_TIMEFRAME_MAP: dict[str, str] = {
    "1m": "5m",
    "5m": "15m",
    "15m": "1h",
    "30m": "2h",
    "1h": "4h",
    "2h": "8h",
    "4h": "1d",
}

# Soglia in unità di ATR: solo controtrend "decisi" (close oltre 0.5 ATR dalla EMA
# nel verso sbagliato) vengono bloccati. Close in zona di indecisione (entro 0.5 ATR
# dalla EMA) passa, evitando di tagliare segnali validi in transizioni di trend.
_MTF_CONTRA_ATR_THRESHOLD = 0.5


def higher_timeframe(timeframe: str) -> str | None:
    return HIGHER_TIMEFRAME_MAP.get(timeframe)


def check_mtf_confluence(
    higher_tf_candles: list[CandleInput],
    direction: Direction,
    *,
    enabled: bool,
    ema_period: int = 50,
    contra_atr_threshold: float = _MTF_CONTRA_ATR_THRESHOLD,
) -> tuple[bool, dict[str, Any] | None]:
    if not enabled or direction == Direction.NEUTRAL:
        return True, None
    if len(higher_tf_candles) < ema_period:
        return True, None
    closes = [candle.close for candle in higher_tf_candles]
    highs = [candle.high for candle in higher_tf_candles]
    lows = [candle.low for candle in higher_tf_candles]
    ema_series = ema(closes, ema_period)
    latest_ema = ema_series[-1]
    if latest_ema is None:
        return True, None
    last_close = closes[-1]
    atr_series = atr(highs, lows, closes, 14)
    latest_atr = atr_series[-1] if atr_series else None
    # Distanza normalizzata in ATR: misura quanto la close è lontana dalla EMA in
    # unità di volatilità tipica. Una distanza di 0.2 ATR è rumore; 1.5 ATR è trend forte.
    distance_atr = (
        (last_close - latest_ema) / latest_atr if latest_atr and latest_atr > 0 else 0.0
    )
    higher_trend = "up" if last_close > latest_ema else "down"
    is_long = direction in {Direction.LONG, Direction.STRONG_LONG}
    is_short = direction in {Direction.SHORT, Direction.STRONG_SHORT}
    info: dict[str, Any] = {
        "close": last_close,
        "ema": latest_ema,
        "atr": latest_atr,
        "distance_atr": distance_atr,
        "higher_trend": higher_trend,
        "direction": direction.value,
        "contra_threshold_atr": contra_atr_threshold,
    }
    # Blocca SOLO se la close è significativamente nel lato "sbagliato" rispetto alla
    # EMA. Vicino alla EMA → regime ambiguo, lascia passare (riduce falsi blocchi nel
    # cambio di trend).
    if is_long and distance_atr < -contra_atr_threshold:
        return False, info
    if is_short and distance_atr > contra_atr_threshold:
        return False, info
    return True, info
