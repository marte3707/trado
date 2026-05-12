from app.core.enums import Direction
from app.indicators.core import vwap
from app.strategies.base import StrategyContext, StrategyResult


class VwapStrategy:
    name = "vwap"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        period = int(params.get("period", 48))
        tolerance_pct = float(params.get("tolerance_pct", 0.002))
        candles = context.candles
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        volumes = [candle.volume for candle in candles]
        values = vwap(highs, lows, closes, volumes, period)
        latest = values[-1] if values else None
        previous = values[-2] if len(values) > 1 else None
        latest_close = closes[-1] if closes else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "VWAP insufficient data"
        if latest is not None and previous is not None and latest_close is not None:
            distance_pct = (latest_close - latest) / latest_close
            rising = latest > previous
            if distance_pct > tolerance_pct and rising:
                direction = Direction.LONG
                confidence = min(78, int(58 + abs(distance_pct) * 2000))
                reason = "Price above rising VWAP"
            elif distance_pct < -tolerance_pct and not rising:
                direction = Direction.SHORT
                confidence = min(78, int(58 + abs(distance_pct) * 2000))
                reason = "Price below falling VWAP"
            else:
                reason = "Price near VWAP"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={"vwap": latest, "previous_vwap": previous},
            suggested_entry=latest_close,
        )
