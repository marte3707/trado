from app.core.enums import Direction
from app.indicators.core import latest_candlestick_pattern
from app.strategies.base import StrategyContext, StrategyResult


class CandlestickPatternStrategy:
    name = "candlestick_patterns"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        candles = context.candles
        opens = [candle.open for candle in candles]
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        pattern = latest_candlestick_pattern(opens, highs, lows, closes)
        raw_direction = pattern.get("direction")
        direction = Direction.NEUTRAL
        if raw_direction == Direction.LONG.value:
            direction = Direction.LONG
        elif raw_direction == Direction.SHORT.value:
            direction = Direction.SHORT
        confidence = int(pattern.get("confidence") or 0)
        pattern_name = pattern.get("pattern")
        reason = f"Candlestick pattern: {pattern_name}" if pattern_name else "No candlestick pattern"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values=pattern,
            suggested_entry=closes[-1] if closes else None,
        )
