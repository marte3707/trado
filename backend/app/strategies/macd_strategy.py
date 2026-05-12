from app.core.enums import Direction
from app.indicators.core import macd
from app.strategies.base import StrategyContext, StrategyResult


class MacdStrategy:
    name = "macd"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        closes = [candle.close for candle in context.candles]
        values = macd(
            closes,
            fast=int(params.get("fast", 12)),
            slow=int(params.get("slow", 26)),
            signal=int(params.get("signal", 9)),
        )
        line = values["macd"][-1] if values["macd"] else None
        signal = values["signal"][-1] if values["signal"] else None
        hist = values["histogram"][-1] if values["histogram"] else None
        prev_hist = values["histogram"][-2] if len(values["histogram"]) > 1 else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "MACD insufficient data"
        if line is not None and signal is not None and hist is not None:
            if line > signal and hist > 0:
                direction = Direction.LONG
                accelerating = prev_hist is not None and hist > prev_hist
                confidence = 80 if accelerating else 65
                reason = "MACD bullish momentum" + (" (accelerating)" if accelerating else "")
            elif line < signal and hist < 0:
                direction = Direction.SHORT
                accelerating = prev_hist is not None and hist < prev_hist
                confidence = 80 if accelerating else 65
                reason = "MACD bearish momentum" + (" (accelerating)" if accelerating else "")
            else:
                reason = "MACD neutral"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={"macd": line, "signal": signal, "histogram": hist},
            suggested_entry=closes[-1] if closes else None,
        )

