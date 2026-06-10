from __future__ import annotations

from app.core.enums import Direction
from app.indicators.core import atr
from app.strategies.base import StrategyContext, StrategyResult


class LiquidationCascadeStrategy:
    """Rileva il completamento di una cascata di liquidazioni via pattern OHLCV.

    Limite noto: Hyperliquid non espone un liquidation feed diretto via candele.
    Approssimiamo il pattern visibile post-cascata:
      1. Volume eccezionale (≥ N× media)
      2. Range estremo (ATR atipico, ≥ M× media)
      3. Wick lungo nella direzione "sbagliata" + close vicino al lato opposto
         del range → il movimento di liquidazione è stato ASSORBITO

    Direzione del segnale:
    - Spike DOWN + close in alto → liquidazione LONG completata, possibile reversal LONG
    - Spike UP + close in basso → liquidazione SHORT completata, possibile reversal SHORT

    Vs Wyckoff Spring/Upthrust: questa strategia non richiede livelli S/R pre-definiti,
    cerca solo il "climax di volume + wick di assorbimento" su qualsiasi swing.
    """

    name = "liquidation_cascade"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        lookback = int(params.get("lookback", 50))
        volume_factor = float(params.get("volume_factor", 2.5))
        range_factor = float(params.get("range_factor", 2.0))
        wick_min_ratio = float(params.get("wick_min_ratio", 0.55))
        close_position_threshold = float(params.get("close_position_threshold", 0.35))

        candles = context.candles
        latest_close = candles[-1].close if candles else None

        if len(candles) < lookback + 15:
            return self._neutral(context, latest_close, "Dati insufficienti per cascata")

        last = candles[-1]
        # Media volume e ATR sulla finestra precedente (escludendo la candela corrente
        # per evitare contamination del segnale dalla candela stessa)
        window = candles[-(lookback + 1) : -1]
        avg_volume = sum(c.volume for c in window) / len(window)
        if avg_volume <= 0:
            return self._neutral(context, latest_close, "Volume medio zero")

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        atr_series = atr(highs, lows, closes, 14)
        latest_atr = atr_series[-2] if atr_series and atr_series[-2] is not None else None
        if latest_atr is None or latest_atr <= 0:
            return self._neutral(context, latest_close, "ATR non disponibile")

        candle_range = last.high - last.low
        volume_ratio = last.volume / avg_volume
        range_ratio = candle_range / latest_atr
        upper_wick = last.high - max(last.open, last.close)
        lower_wick = min(last.open, last.close) - last.low
        # Posizione del close nel range [0, 1]: 0 = sul minimo, 1 = sul massimo
        close_position = (last.close - last.low) / candle_range if candle_range > 0 else 0.5

        indicator_values = {
            "volume_ratio": round(volume_ratio, 2),
            "range_ratio": round(range_ratio, 2),
            "upper_wick_ratio": round(upper_wick / candle_range, 3) if candle_range > 0 else 0.0,
            "lower_wick_ratio": round(lower_wick / candle_range, 3) if candle_range > 0 else 0.0,
            "close_position": round(close_position, 3),
            "avg_volume": round(avg_volume, 2),
            "atr": round(latest_atr, 6),
        }

        # Pre-condizioni: serve volume + range fuori scala per parlare di cascata
        if volume_ratio < volume_factor or range_ratio < range_factor:
            return self._neutral(
                context, latest_close, "No volume/range climax", indicator_values
            )

        if candle_range <= 0:
            return self._neutral(context, latest_close, "Range nullo", indicator_values)

        lower_wick_ratio = lower_wick / candle_range
        upper_wick_ratio = upper_wick / candle_range

        # LONG: spike down assorbito → wick basso lungo + close nella metà ALTA
        if lower_wick_ratio >= wick_min_ratio and close_position >= (1 - close_position_threshold):
            confidence = min(85, 55 + int((volume_ratio - volume_factor) * 8) + int((range_ratio - range_factor) * 6))
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.LONG,
                confidence=confidence,
                reason=(
                    f"Long liquidation cascade absorbed: vol {volume_ratio:.1f}× avg, "
                    f"range {range_ratio:.1f}× ATR, lower wick {lower_wick_ratio:.0%}"
                ),
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        # SHORT: spike up assorbito → wick alto lungo + close nella metà BASSA
        if upper_wick_ratio >= wick_min_ratio and close_position <= close_position_threshold:
            confidence = min(85, 55 + int((volume_ratio - volume_factor) * 8) + int((range_ratio - range_factor) * 6))
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.SHORT,
                confidence=confidence,
                reason=(
                    f"Short liquidation cascade absorbed: vol {volume_ratio:.1f}× avg, "
                    f"range {range_ratio:.1f}× ATR, upper wick {upper_wick_ratio:.0%}"
                ),
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        return self._neutral(
            context, latest_close, "Volume/range climax senza wick di assorbimento", indicator_values
        )

    def _neutral(
        self,
        context: StrategyContext,
        latest_close: float | None,
        reason: str,
        indicator_values: dict | None = None,
    ) -> StrategyResult:
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=Direction.NEUTRAL,
            confidence=0,
            reason=reason,
            indicator_values=indicator_values or {},
            suggested_entry=latest_close,
        )
