from app.core.enums import Direction
from app.indicators.core import supertrend
from app.strategies.base import StrategyContext, StrategyResult


class SupertrendStrategy:
    name = "supertrend"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        period = int(params.get("period", 10))
        multiplier = float(params.get("multiplier", 3.0))
        candles = context.candles
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        values = supertrend(highs, lows, closes, period, multiplier)
        latest_direction = values["direction"][-1] if values["direction"] else None
        previous_direction = values["direction"][-2] if len(values["direction"]) > 1 else None
        trend_value = values["trend"][-1] if values["trend"] else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "Supertrend insufficient data"
        if latest_direction == "up":
            direction = Direction.LONG
            confidence = 82 if previous_direction == "down" else 62
            reason = "Supertrend bullish flip" if previous_direction == "down" else "Supertrend bullish"
        elif latest_direction == "down":
            direction = Direction.SHORT
            confidence = 82 if previous_direction == "up" else 62
            reason = "Supertrend bearish flip" if previous_direction == "up" else "Supertrend bearish"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "direction": latest_direction,
                "previous_direction": previous_direction,
                "trend": trend_value,
                "upper": values["upper"][-1] if values["upper"] else None,
                "lower": values["lower"][-1] if values["lower"] else None,
            },
            suggested_entry=closes[-1] if closes else None,
        )
