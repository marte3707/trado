from __future__ import annotations

from typing import Any

from app.core.enums import Direction
from app.indicators.core import ema
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


def higher_timeframe(timeframe: str) -> str | None:
    return HIGHER_TIMEFRAME_MAP.get(timeframe)


def check_mtf_confluence(
    higher_tf_candles: list[CandleInput],
    direction: Direction,
    *,
    enabled: bool,
    ema_period: int = 50,
) -> tuple[bool, dict[str, Any] | None]:
    if not enabled or direction == Direction.NEUTRAL:
        return True, None
    if len(higher_tf_candles) < ema_period:
        return True, None
    closes = [candle.close for candle in higher_tf_candles]
    ema_series = ema(closes, ema_period)
    latest_ema = ema_series[-1]
    if latest_ema is None:
        return True, None
    last_close = closes[-1]
    higher_trend = "up" if last_close > latest_ema else "down"
    is_long = direction in {Direction.LONG, Direction.STRONG_LONG}
    is_short = direction in {Direction.SHORT, Direction.STRONG_SHORT}
    if is_long and higher_trend == "down":
        return False, {
            "close": last_close,
            "ema": latest_ema,
            "higher_trend": higher_trend,
            "direction": direction.value,
        }
    if is_short and higher_trend == "up":
        return False, {
            "close": last_close,
            "ema": latest_ema,
            "higher_trend": higher_trend,
            "direction": direction.value,
        }
    return True, {
        "close": last_close,
        "ema": latest_ema,
        "higher_trend": higher_trend,
        "direction": direction.value,
    }
