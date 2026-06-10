from __future__ import annotations

from app.core.enums import Direction
from app.indicators.volume_profile import VolumeProfileResult, compute_volume_profile
from app.strategies.base import StrategyContext, StrategyResult


class VolumeProfileStrategy:
    """Volume Profile (VPVR) — interazione del prezzo con livelli ad alto volume.

    Costruisce un istogramma di volume per fasce di prezzo sulla finestra di
    lookback. Senza accesso ai tick-by-tick volumes, distribuisce il volume di
    ogni candela uniformemente tra `low` e `high` (approssimazione standard
    "TPO-volume-style" usata da molte piattaforme retail).

    Concetti chiave:
    - **POC** (Point of Control): bin con volume massimo — livello di equilibrio
    - **HVN** (High Volume Node): bin nei top 20% — supporti/resistenze "forti"
    - **LVN** (Low Volume Node): bin nei bottom 20% — zone di transizione veloce

    Segnale:
    - LONG quando il prezzo viene da SOTTO il POC e lo sta riprendendo
      (close > POC > previous close, con close vicino al POC)
    - SHORT quando il prezzo viene da SOPRA il POC e lo sta perdendo
      (close < POC < previous close)
    - Confidence bonus se il POC è anche un HVN (volume picco rilevante).
    """

    name = "volume_profile"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        lookback = int(params.get("lookback", 100))
        bins = int(params.get("bins", 30))
        proximity_pct = float(params.get("proximity_pct", 0.004))
        hvn_percentile = float(params.get("hvn_percentile", 0.80))
        lvn_percentile = float(params.get("lvn_percentile", 0.20))
        value_area_pct = float(params.get("value_area_pct", 0.70))
        tick_size = params.get("tick_size")
        parsed_tick_size = float(tick_size) if tick_size is not None else None

        candles = context.candles
        latest_close = candles[-1].close if candles else None

        if len(candles) < lookback + 2:
            return self._neutral(context, latest_close, "Dati volume profile insufficienti")
        if bins < 5:
            return self._neutral(context, latest_close, "Numero di bin troppo basso")

        profile = compute_volume_profile(
            candles[-lookback:],
            bins=bins,
            value_area_pct=value_area_pct,
            hvn_percentile=hvn_percentile,
            lvn_percentile=lvn_percentile,
            tick_size=parsed_tick_size,
        )
        if profile is None:
            return self._neutral(context, latest_close, "Volume totale zero")

        previous_close = candles[-2].close
        latest_close_val = candles[-1].close
        latest_high = candles[-1].high
        latest_low = candles[-1].low

        distance_pct = self._distance_pct(latest_close_val, profile.poc_price)
        near_poc = distance_pct <= proximity_pct
        distance_vah_pct = self._distance_pct(latest_close_val, profile.vah_price)
        distance_val_pct = self._distance_pct(latest_close_val, profile.val_price)
        near_vah = distance_vah_pct <= proximity_pct
        near_val = distance_val_pct <= proximity_pct

        indicator_values = self._indicator_values(
            profile,
            bins=bins,
            distance_pct=distance_pct,
            distance_vah_pct=distance_vah_pct,
            distance_val_pct=distance_val_pct,
        )

        if not (near_poc or near_vah or near_val):
            return self._neutral(
                context,
                latest_close,
                f"Prezzo lontano da POC/VA ({min(distance_pct, distance_vah_pct, distance_val_pct):.2%})",
                indicator_values,
            )

        # Reclaim/loss del POC: usa il previous close come riferimento del "venendo da..."
        reclaim_long = previous_close < profile.poc_price and latest_close_val > profile.poc_price
        loss_short = previous_close > profile.poc_price and latest_close_val < profile.poc_price

        if reclaim_long:
            confidence = 65 + (10 if profile.poc_is_hvn else 0)
            confidence = min(80, confidence)
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.LONG,
                confidence=confidence,
                reason=f"POC reclaim long ({'HVN' if profile.poc_is_hvn else 'standard'} at {profile.poc_price:.6g})",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        if loss_short:
            confidence = 65 + (10 if profile.poc_is_hvn else 0)
            confidence = min(80, confidence)
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.SHORT,
                confidence=confidence,
                reason=f"POC loss short ({'HVN' if profile.poc_is_hvn else 'standard'} at {profile.poc_price:.6g})",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        vah_breakout_long = previous_close <= profile.vah_price < latest_close_val and near_vah
        val_loss_short = previous_close >= profile.val_price > latest_close_val and near_val
        vah_reentry_short = previous_close > profile.vah_price and latest_close_val < profile.vah_price and near_vah
        val_reentry_long = previous_close < profile.val_price and latest_close_val > profile.val_price and near_val
        vah_rejection_short = latest_high > profile.vah_price > latest_close_val and near_vah
        val_rejection_long = latest_low < profile.val_price < latest_close_val and near_val

        if vah_breakout_long:
            return self._directional(
                context,
                Direction.LONG,
                68,
                f"VAH breakout accepted at {profile.vah_price:.6g}",
                latest_close,
                indicator_values,
            )
        if val_loss_short:
            return self._directional(
                context,
                Direction.SHORT,
                68,
                f"VAL loss accepted at {profile.val_price:.6g}",
                latest_close,
                indicator_values,
            )
        if vah_reentry_short or vah_rejection_short:
            return self._directional(
                context,
                Direction.SHORT,
                64,
                f"VAH rejection/reentry into value area at {profile.vah_price:.6g}",
                latest_close,
                indicator_values,
            )
        if val_reentry_long or val_rejection_long:
            return self._directional(
                context,
                Direction.LONG,
                64,
                f"VAL rejection/reentry into value area at {profile.val_price:.6g}",
                latest_close,
                indicator_values,
            )

        return self._neutral(
            context, latest_close, "Vicino a POC/VA ma nessuna transizione", indicator_values
        )

    @staticmethod
    def _distance_pct(price: float, level: float) -> float:
        return abs(price - level) / price if price else 0.0

    @staticmethod
    def _indicator_values(
        profile: VolumeProfileResult,
        *,
        bins: int,
        distance_pct: float,
        distance_vah_pct: float,
        distance_val_pct: float,
    ) -> dict:
        return {
            "source": profile.source,
            "poc": round(profile.poc_price, 6),
            "poc_volume": round(profile.poc_volume, 2),
            "poc_is_hvn": profile.poc_is_hvn,
            "vah": round(profile.vah_price, 6),
            "val": round(profile.val_price, 6),
            "value_area_pct": round(profile.value_area_pct * 100, 2),
            "value_area_volume": round(profile.value_area_volume, 2),
            "distance_from_poc_pct": round(distance_pct * 100, 3),
            "distance_from_vah_pct": round(distance_vah_pct * 100, 3),
            "distance_from_val_pct": round(distance_val_pct * 100, 3),
            "bins": bins,
            "actual_bins": len(profile.levels),
            "bin_width": round(profile.bin_width, 8),
            "price_min": round(profile.price_min, 6),
            "price_max": round(profile.price_max, 6),
            "total_volume": round(profile.total_volume, 2),
            "delta_volume": round(profile.delta_volume, 2),
            "hvn_prices": [round(price, 6) for price in profile.hvn_prices[:8]],
            "lvn_prices": [round(price, 6) for price in profile.lvn_prices[:8]],
            "top_levels": profile.compact_levels(limit=8),
        }

    def _directional(
        self,
        context: StrategyContext,
        direction: Direction,
        confidence: int,
        reason: str,
        latest_close: float | None,
        indicator_values: dict,
    ) -> StrategyResult:
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
