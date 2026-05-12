from app.core.enums import Direction
from app.indicators.core import simple_moving_average
from app.strategies.base import StrategyContext, StrategyResult


class VolumeSpikeStrategy:
    name = "volume_spike"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        period = int(params.get("period", 20))
        multiplier = float(params.get("multiplier", 1.5))
        candles = context.candles
        volumes = [candle.volume for candle in candles]
        average_values = simple_moving_average(volumes, period)
        latest_average = average_values[-2] if len(average_values) > 1 else None
        latest_volume = volumes[-1] if volumes else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "Volume insufficient data"
        if latest_average and latest_volume:
            candle = candles[-1]
            is_spike = latest_volume >= latest_average * multiplier
            if is_spike and candle.close > candle.open:
                direction = Direction.LONG
                ratio = latest_volume / latest_average
                confidence = min(95, int(50 + (ratio - 1) * 20))
                reason = f"Bullish volume spike ({ratio:.1f}x avg)"
            elif is_spike and candle.close < candle.open:
                direction = Direction.SHORT
                ratio = latest_volume / latest_average
                confidence = min(95, int(50 + (ratio - 1) * 20))
                reason = f"Bearish volume spike ({ratio:.1f}x avg)"
            else:
                reason = "No volume spike"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={"volume": latest_volume, "volume_ma": latest_average},
            suggested_entry=candles[-1].close if candles else None,
        )

