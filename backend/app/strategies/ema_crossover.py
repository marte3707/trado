from app.core.enums import Direction
from app.indicators.core import ema
from app.strategies.base import StrategyContext, StrategyResult


class EmaCrossoverStrategy:
    name = "ema_crossover"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        fast = int(params.get("fast", 9))
        slow = int(params.get("slow", 21))
        trend = int(params.get("trend", 200))
        closes = [candle.close for candle in context.candles]
        fast_values = ema(closes, fast)
        slow_values = ema(closes, slow)
        trend_values = ema(closes, trend)
        latest_fast = fast_values[-1]
        latest_slow = slow_values[-1]
        previous_fast = fast_values[-2] if len(fast_values) > 1 else None
        previous_slow = slow_values[-2] if len(slow_values) > 1 else None
        latest_trend = trend_values[-1]
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "EMA insufficient data"
        if (
            latest_fast is not None
            and latest_slow is not None
            and previous_fast is not None
            and previous_slow is not None
        ):
            bullish_cross = previous_fast <= previous_slow and latest_fast > latest_slow
            bearish_cross = previous_fast >= previous_slow and latest_fast < latest_slow
            above_trend = latest_trend is not None and closes[-1] > latest_trend
            below_trend = latest_trend is not None and closes[-1] < latest_trend
            if bullish_cross:
                direction = Direction.LONG
                confidence = 85 if above_trend else 70
                reason = f"EMA {fast}/{slow} bullish crossover"
            elif bearish_cross:
                direction = Direction.SHORT
                confidence = 85 if below_trend else 70
                reason = f"EMA {fast}/{slow} bearish crossover"
            elif latest_fast > latest_slow and above_trend:
                direction = Direction.LONG
                confidence = 60
                reason = f"EMA {fast} above EMA {slow} and price above EMA {trend}"
            elif latest_fast < latest_slow and below_trend:
                direction = Direction.SHORT
                confidence = 60
                reason = f"EMA {fast} below EMA {slow} and price below EMA {trend}"
            else:
                reason = "EMA trend not aligned"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                f"ema_{fast}": latest_fast,
                f"ema_{slow}": latest_slow,
                f"ema_{trend}": latest_trend,
            },
            suggested_entry=closes[-1] if closes else None,
        )
