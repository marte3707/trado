from app.core.enums import Direction
from app.indicators.core import bollinger_bands
from app.strategies.base import StrategyContext, StrategyResult


class BollingerBandsStrategy:
    name = "bollinger_bands"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        period = int(params.get("period", 20))
        stddev = float(params.get("stddev", 2.0))
        candles = context.candles
        closes = [candle.close for candle in candles]
        bands = bollinger_bands(closes, period, stddev)
        latest_close = closes[-1] if closes else None
        lower = bands["lower"][-1] if bands["lower"] else None
        upper = bands["upper"][-1] if bands["upper"] else None
        middle = bands["middle"][-1] if bands["middle"] else None
        percent_b = bands["percent_b"][-1] if bands["percent_b"] else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "Bollinger Bands insufficient data"
        if latest_close is not None and lower is not None and upper is not None:
            previous_close = closes[-2] if len(closes) > 1 else latest_close
            if previous_close < lower and latest_close > lower:
                direction = Direction.LONG
                confidence = 70
                reason = "Close reclaimed lower Bollinger Band"
            elif previous_close > upper and latest_close < upper:
                direction = Direction.SHORT
                confidence = 70
                reason = "Close rejected upper Bollinger Band"
            elif latest_close < lower:
                direction = Direction.LONG
                confidence = 55
                reason = "Price stretched below lower Bollinger Band"
            elif latest_close > upper:
                direction = Direction.SHORT
                confidence = 55
                reason = "Price stretched above upper Bollinger Band"
            else:
                reason = "Price inside Bollinger Bands"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "middle": middle,
                "upper": upper,
                "lower": lower,
                "percent_b": percent_b,
            },
            suggested_entry=latest_close,
        )
