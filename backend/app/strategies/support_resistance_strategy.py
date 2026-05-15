from app.core.enums import Direction
from app.indicators.core import support_resistance_levels
from app.strategies.base import StrategyContext, StrategyResult


class SupportResistanceStrategy:
    name = "support_resistance"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        lookback = int(params.get("lookback", 120))
        pivot_window = int(params.get("pivot_window", 3))
        tolerance_pct = float(params.get("tolerance_pct", 0.0035))
        candles = context.candles
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        latest_close = closes[-1] if closes else None
        levels = support_resistance_levels(highs, lows, lookback, pivot_window, tolerance_pct)
        supports = levels["supports"]
        resistances = levels["resistances"]
        direction = Direction.NEUTRAL
        confidence = 0
        reason = "Support/resistance insufficient data"
        nearest_support = _nearest_below(supports, latest_close) if latest_close else None
        nearest_resistance = _nearest_above(resistances, latest_close) if latest_close else None
        if latest_close is not None and candles:
            candle = candles[-1]
            previous_close = closes[-2] if len(closes) > 1 else latest_close
            previous_resistance = _nearest_above(resistances, previous_close)
            previous_support = _nearest_below(supports, previous_close)
            support_distance = (
                abs(latest_close - nearest_support) / latest_close if nearest_support else None
            )
            resistance_distance = (
                abs(nearest_resistance - latest_close) / latest_close if nearest_resistance else None
            )
            broke_resistance = previous_resistance is not None and latest_close > previous_resistance
            broke_support = previous_support is not None and latest_close < previous_support
            rejected_support = (
                support_distance is not None
                and support_distance <= tolerance_pct
                and nearest_support is not None
                and candle.low <= nearest_support
                and candle.close > candle.open
            )
            rejected_resistance = (
                resistance_distance is not None
                and resistance_distance <= tolerance_pct
                and nearest_resistance is not None
                and candle.high >= nearest_resistance
                and candle.close < candle.open
            )
            if rejected_support:
                direction = Direction.LONG
                confidence = 72
                reason = "Bullish rejection from support"
            elif rejected_resistance:
                direction = Direction.SHORT
                confidence = 72
                reason = "Bearish rejection from resistance"
            elif broke_resistance and candle.close > candle.open:
                direction = Direction.LONG
                confidence = 65
                reason = "Bullish breakout above resistance"
            elif broke_support and candle.close < candle.open:
                direction = Direction.SHORT
                confidence = 65
                reason = "Bearish breakdown below support"
            else:
                reason = "No actionable support/resistance reaction"
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values={
                "nearest_support": nearest_support,
                "nearest_resistance": nearest_resistance,
                "supports": supports[-5:],
                "resistances": resistances[:5],
            },
            suggested_entry=latest_close,
        )


def _nearest_below(levels: list[float], price: float | None) -> float | None:
    if price is None:
        return None
    below = [level for level in levels if level <= price]
    return max(below) if below else None


def _nearest_above(levels: list[float], price: float | None) -> float | None:
    if price is None:
        return None
    above = [level for level in levels if level >= price]
    return min(above) if above else None
