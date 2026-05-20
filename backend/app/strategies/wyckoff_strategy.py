from app.core.enums import Direction
from app.indicators.core import wyckoff_spring_upthrust
from app.strategies.base import StrategyContext, StrategyResult


class WyckoffSpringUpthrustStrategy:
    name = "wyckoff_spring_upthrust"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        candles = context.candles
        lookback = int(params.get("lookback", 50))
        pivot_window = int(params.get("pivot_window", 5))
        tolerance_pct = float(params.get("tolerance_pct", 0.002))
        volume_factor = float(params.get("volume_factor", 1.3))

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        opens = [c.open for c in candles]
        volumes = [c.volume for c in candles]

        result = wyckoff_spring_upthrust(
            highs,
            lows,
            closes,
            opens,
            volumes,
            lookback=lookback,
            pivot_window=pivot_window,
            tolerance_pct=tolerance_pct,
            volume_factor=volume_factor,
        )

        pattern = result.get("pattern")
        raw_direction = result.get("direction", "NEUTRAL")
        direction = Direction.NEUTRAL
        if raw_direction == "LONG":
            direction = Direction.LONG
        elif raw_direction == "SHORT":
            direction = Direction.SHORT

        confidence = int(result.get("confidence", 0))

        if pattern == "spring":
            support = result.get("support_level")
            penetration = result.get("penetration_pct", 0)
            reason = f"Wyckoff Spring: falso breakdown sotto supporto {support} ({penetration:.3f}% penetrazione), close recuperato sopra"
        elif pattern == "upthrust":
            resistance = result.get("resistance_level")
            penetration = result.get("penetration_pct", 0)
            reason = f"Wyckoff Upthrust: falso breakout sopra resistenza {resistance} ({penetration:.3f}% penetrazione), close respinto sotto"
        else:
            reason = "Nessun pattern Wyckoff Spring/Upthrust"

        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values=result,
            suggested_entry=closes[-1] if closes else None,
        )
