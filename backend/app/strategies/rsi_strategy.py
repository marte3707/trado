from app.core.enums import Direction
from app.indicators.core import ema, rsi
from app.strategies.base import StrategyContext, StrategyResult


class RsiStrategy:
    name = "rsi"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        period = int(params.get("period", 14))
        oversold = float(params.get("oversold", 30))
        overbought = float(params.get("overbought", 70))
        closes = [candle.close for candle in context.candles]
        values = rsi(closes, period)
        latest = values[-1] if values else None
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "RSI insufficient data"
        if latest is not None:
            ema50_vals = ema(closes, 50)
            ema200_vals = ema(closes, 200)
            ema50 = ema50_vals[-1] if ema50_vals else None
            ema200 = ema200_vals[-1] if ema200_vals else None
            macro_uptrend = ema50 is not None and ema200 is not None and ema50 > ema200
            macro_downtrend = ema50 is not None and ema200 is not None and ema50 < ema200
            if latest <= oversold:
                if macro_uptrend:
                    direction = Direction.LONG
                    confidence = min(90, int(50 + (oversold - latest) * 2))
                    reason = f"RSI oversold at {latest:.2f} (uptrend confirmed)"
                else:
                    reason = f"RSI oversold at {latest:.2f} (blocked: no uptrend)"
            elif latest >= overbought:
                if macro_downtrend:
                    direction = Direction.SHORT
                    confidence = min(90, int(50 + (latest - overbought) * 2))
                    reason = f"RSI overbought at {latest:.2f} (downtrend confirmed)"
                else:
                    reason = f"RSI overbought at {latest:.2f} (blocked: no downtrend)"
            else:
                reason = f"RSI neutral at {latest:.2f}"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={"rsi": latest},
            suggested_entry=closes[-1] if closes else None,
        )

