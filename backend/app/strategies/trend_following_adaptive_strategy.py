from __future__ import annotations

from app.core.enums import Direction
from app.indicators.core import adx, atr, ema
from app.strategies.base import StrategyContext, StrategyResult


class TrendFollowingAdaptiveStrategy:
    """Conservative trend-following strategy for liquid crypto perps.

    The signal requires three independent confirmations:
    - EMA stack agrees with direction.
    - Price breaks the previous Donchian channel.
    - ADX confirms a non-choppy trend.

    ATR is used only for adaptive breakout context and suggested risk levels;
    final sizing and SL/TP validation stay in RiskManager.
    """

    name = "trend_following_adaptive"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        fast_ema = int(params.get("fast_ema", 21))
        slow_ema = int(params.get("slow_ema", 55))
        trend_ema = int(params.get("trend_ema", 200))
        donchian_lookback = int(params.get("donchian_lookback", 55))
        adx_period = int(params.get("adx_period", 14))
        min_adx = float(params.get("min_adx", 18.0))
        atr_period = int(params.get("atr_period", 14))
        atr_stop_multiplier = float(params.get("atr_stop_multiplier", 2.5))
        reward_r_multiple = float(params.get("reward_r_multiple", 2.0))
        volume_lookback = int(params.get("volume_lookback", 20))
        min_volume_ratio = float(params.get("min_volume_ratio", 0.8))
        breakout_atr_buffer = float(params.get("breakout_atr_buffer", 0.05))

        candles = context.candles
        latest_close = candles[-1].close if candles else None
        min_required = max(
            fast_ema,
            slow_ema,
            trend_ema,
            donchian_lookback + 1,
            adx_period * 2 + 1,
            atr_period,
            volume_lookback,
        )
        if len(candles) < min_required:
            return self._neutral(
                context,
                latest_close,
                f"Trend-following insufficient data ({len(candles)}/{min_required})",
            )

        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        volumes = [candle.volume for candle in candles]

        latest_index = len(candles) - 1
        fast_values = ema(closes, fast_ema)
        slow_values = ema(closes, slow_ema)
        trend_values = ema(closes, trend_ema)
        adx_values = adx(highs, lows, closes, adx_period)
        atr_values = atr(highs, lows, closes, atr_period)

        latest_fast = fast_values[latest_index]
        latest_slow = slow_values[latest_index]
        latest_trend = trend_values[latest_index]
        latest_adx = adx_values[latest_index]
        latest_atr = atr_values[latest_index]

        if (
            latest_close is None
            or latest_fast is None
            or latest_slow is None
            or latest_trend is None
            or latest_adx is None
            or latest_atr is None
            or latest_atr <= 0
        ):
            return self._neutral(
                context,
                latest_close,
                "Trend-following indicators not ready",
                self._indicator_values(
                    latest_fast,
                    latest_slow,
                    latest_trend,
                    latest_adx,
                    latest_atr,
                    None,
                    None,
                    None,
                    None,
                ),
            )

        previous_high = max(highs[-donchian_lookback - 1 : -1])
        previous_low = min(lows[-donchian_lookback - 1 : -1])
        breakout_buffer = latest_atr * max(0.0, breakout_atr_buffer)
        average_volume = sum(volumes[-volume_lookback - 1 : -1]) / volume_lookback
        volume_ratio = volumes[-1] / average_volume if average_volume > 0 else None
        long_aligned = latest_close > latest_fast > latest_slow > latest_trend
        short_aligned = latest_close < latest_fast < latest_slow < latest_trend
        long_breakout = latest_close > previous_high + breakout_buffer
        short_breakout = latest_close < previous_low - breakout_buffer
        trend_ready = latest_adx >= min_adx
        volume_ready = volume_ratio is None or volume_ratio >= min_volume_ratio
        indicator_values = self._indicator_values(
            latest_fast,
            latest_slow,
            latest_trend,
            latest_adx,
            latest_atr,
            previous_high,
            previous_low,
            breakout_buffer,
            volume_ratio,
        )

        if long_aligned and long_breakout and trend_ready and volume_ready:
            return self._directional(
                context,
                Direction.LONG,
                self._confidence(latest_adx, min_adx, volume_ratio, latest_close, latest_trend, latest_atr),
                "Adaptive trend long breakout",
                latest_close,
                latest_atr,
                atr_stop_multiplier,
                reward_r_multiple,
                indicator_values,
            )
        if short_aligned and short_breakout and trend_ready and volume_ready:
            return self._directional(
                context,
                Direction.SHORT,
                self._confidence(latest_adx, min_adx, volume_ratio, latest_close, latest_trend, latest_atr),
                "Adaptive trend short breakout",
                latest_close,
                latest_atr,
                atr_stop_multiplier,
                reward_r_multiple,
                indicator_values,
            )

        blockers: list[str] = []
        if not trend_ready:
            blockers.append("weak_adx")
        if not volume_ready:
            blockers.append("low_volume")
        if not (long_aligned or short_aligned):
            blockers.append("ema_stack_not_aligned")
        if not (long_breakout or short_breakout):
            blockers.append("no_donchian_breakout")
        reason = "Trend-following no setup"
        if blockers:
            reason = f"{reason}: {', '.join(blockers)}"
        return self._neutral(context, latest_close, reason, indicator_values)

    @staticmethod
    def _confidence(
        latest_adx: float,
        min_adx: float,
        volume_ratio: float | None,
        latest_close: float,
        trend_value: float,
        latest_atr: float,
    ) -> int:
        confidence = 68
        if latest_adx >= min_adx + 10:
            confidence += 8
        elif latest_adx >= min_adx + 5:
            confidence += 4
        if volume_ratio is not None and volume_ratio >= 1.2:
            confidence += 5
        trend_distance_atr = abs(latest_close - trend_value) / latest_atr if latest_atr else 0.0
        if trend_distance_atr >= 3:
            confidence -= 5
        elif trend_distance_atr >= 1:
            confidence += 4
        return max(0, min(88, confidence))

    @staticmethod
    def _indicator_values(
        fast_value: float | None,
        slow_value: float | None,
        trend_value: float | None,
        adx_value: float | None,
        atr_value: float | None,
        donchian_high: float | None,
        donchian_low: float | None,
        breakout_buffer: float | None,
        volume_ratio: float | None,
    ) -> dict:
        return {
            "ema_fast": round(fast_value, 8) if fast_value is not None else None,
            "ema_slow": round(slow_value, 8) if slow_value is not None else None,
            "ema_trend": round(trend_value, 8) if trend_value is not None else None,
            "adx": round(adx_value, 4) if adx_value is not None else None,
            "atr": round(atr_value, 8) if atr_value is not None else None,
            "donchian_high": round(donchian_high, 8) if donchian_high is not None else None,
            "donchian_low": round(donchian_low, 8) if donchian_low is not None else None,
            "breakout_buffer": round(breakout_buffer, 8) if breakout_buffer is not None else None,
            "volume_ratio": round(volume_ratio, 4) if volume_ratio is not None else None,
        }

    def _directional(
        self,
        context: StrategyContext,
        direction: Direction,
        confidence: int,
        reason: str,
        latest_close: float,
        latest_atr: float,
        atr_stop_multiplier: float,
        reward_r_multiple: float,
        indicator_values: dict,
    ) -> StrategyResult:
        risk_distance = latest_atr * max(0.1, atr_stop_multiplier)
        if direction == Direction.LONG:
            stop_loss = latest_close - risk_distance
            take_profit = latest_close + risk_distance * reward_r_multiple
        else:
            stop_loss = latest_close + risk_distance
            take_profit = latest_close - risk_distance * reward_r_multiple
        return StrategyResult(
            strategy_name=self.name,
            market=context.market_type,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            confidence=confidence,
            reason=reason,
            indicator_values=indicator_values,
            suggested_entry=latest_close,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            invalidation_level=stop_loss,
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
