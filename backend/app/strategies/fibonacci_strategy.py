from app.core.enums import Direction
from app.indicators.core import fibonacci_levels
from app.strategies.base import StrategyContext, StrategyResult


class FibonacciStrategy:
    name = "fibonacci"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        lookback = int(params.get("lookback", 120))
        tolerance_pct = float(params.get("tolerance_pct", 0.004))
        candles = context.candles
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        levels = fibonacci_levels(highs, lows, lookback)
        latest_close = closes[-1] if closes else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "Fibonacci insufficient data"
        nearest_level = None
        if latest_close is not None and isinstance(levels.get("levels"), dict):
            fib_levels = levels["levels"]
            if not fib_levels:
                return StrategyResult(
                    strategy_name=self.name,
                    market=context.market_type,
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    direction=direction,
                    confidence=confidence,
                    reason=reason,
                    indicator_values=levels,
                    suggested_entry=latest_close,
                )
            nearest_name, nearest_level = min(
                fib_levels.items(),
                key=lambda item: abs(float(item[1]) - latest_close),
            )
            distance_pct = abs(float(nearest_level) - latest_close) / latest_close
            previous_close = closes[-2] if len(closes) > 1 else latest_close
            trend = levels.get("trend")
            if distance_pct <= tolerance_pct and trend == "up" and latest_close >= previous_close:
                direction = Direction.LONG
                confidence = max(52, int(76 - (distance_pct / tolerance_pct) * 18))
                reason = f"Price reacting at Fibonacci {nearest_name} retracement in uptrend"
            elif distance_pct <= tolerance_pct and trend == "down" and latest_close <= previous_close:
                direction = Direction.SHORT
                confidence = max(52, int(76 - (distance_pct / tolerance_pct) * 18))
                reason = f"Price reacting at Fibonacci {nearest_name} retracement in downtrend"
            else:
                reason = "Price not reacting at a relevant Fibonacci level"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "trend": levels.get("trend"),
                "swing_high": levels.get("swing_high"),
                "swing_low": levels.get("swing_low"),
                "nearest_level": nearest_level,
                "levels": levels.get("levels", {}),
            },
            suggested_entry=latest_close,
        )
