from __future__ import annotations

from app.core.enums import Direction
from app.strategies.base import StrategyContext, StrategyResult


class OITrendStrategy:
    name = "oi_trend"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        min_candles: int = int(params.get("min_candles", 5))
        oi_change_threshold: float = float(params.get("oi_change_threshold", 0.02))
        price_change_threshold: float = float(params.get("price_change_threshold", 0.001))

        candles_with_oi = [c for c in context.candles if c.open_interest is not None]

        neutral_result = StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=Direction.NEUTRAL,
            confidence=0,
            reason="Dati OI insufficienti",
            indicator_values={"oi_candles": len(candles_with_oi)},
            suggested_entry=context.candles[-1].close if context.candles else None,
        )

        if len(candles_with_oi) < min_candles:
            return neutral_result

        recent = candles_with_oi[-min_candles:]
        oi_start = recent[0].open_interest
        oi_end = recent[-1].open_interest

        if oi_start is None or oi_end is None or oi_start <= 0:
            return neutral_result

        oi_change_pct = (oi_end - oi_start) / oi_start
        price_start = recent[0].close
        price_end = recent[-1].close
        price_change_pct = (price_end - price_start) / price_start if price_start > 0 else 0.0

        oi_growing = oi_change_pct > oi_change_threshold
        oi_shrinking = oi_change_pct < -oi_change_threshold
        price_up = price_change_pct > price_change_threshold
        price_down = price_change_pct < -price_change_threshold

        indicator_values = {
            "oi_start": round(oi_start),
            "oi_end": round(oi_end),
            "oi_change_pct": round(oi_change_pct, 4),
            "price_change_pct": round(price_change_pct, 4),
            "candles_with_oi": len(candles_with_oi),
        }

        if oi_growing and price_up:
            # Nuove posizioni long aperte — momentum confermato
            confidence = min(80, int(40 + abs(oi_change_pct) * 500 + abs(price_change_pct) * 300))
            direction = Direction.LONG
            reason = f"OI +{oi_change_pct:.1%}, prezzo +{price_change_pct:.1%}: nuove posizioni long"
        elif oi_growing and price_down:
            # Nuove posizioni short aperte — momentum short confermato
            confidence = min(80, int(40 + abs(oi_change_pct) * 500 + abs(price_change_pct) * 300))
            direction = Direction.SHORT
            reason = f"OI +{oi_change_pct:.1%}, prezzo {price_change_pct:.1%}: nuove posizioni short"
        elif oi_shrinking and price_up:
            # Short covering — rimbalzo debole, non trend genuino
            confidence = min(55, int(25 + abs(oi_change_pct) * 300))
            direction = Direction.LONG
            reason = f"OI {oi_change_pct:.1%}: short covering (rimbalzo tecnico)"
        elif oi_shrinking and price_down:
            # Long liquidati — capitolazione debole
            confidence = min(55, int(25 + abs(oi_change_pct) * 300))
            direction = Direction.SHORT
            reason = f"OI {oi_change_pct:.1%}: liquidazione long (pressione tecnica)"
        else:
            direction = Direction.NEUTRAL
            confidence = 0
            reason = f"OI stabile ({oi_change_pct:.1%})"

        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values=indicator_values,
            suggested_entry=context.candles[-1].close if context.candles else None,
        )
