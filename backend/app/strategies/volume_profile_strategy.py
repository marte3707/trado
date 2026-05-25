from __future__ import annotations

from app.core.enums import Direction
from app.strategies.base import CandleInput, StrategyContext, StrategyResult


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

        candles = context.candles
        latest_close = candles[-1].close if candles else None

        if len(candles) < lookback + 2:
            return self._neutral(context, latest_close, "Dati volume profile insufficienti")
        if bins < 5:
            return self._neutral(context, latest_close, "Numero di bin troppo basso")

        window = candles[-lookback:]
        all_highs = [c.high for c in window]
        all_lows = [c.low for c in window]
        price_min = min(all_lows)
        price_max = max(all_highs)
        price_range = price_max - price_min
        if price_range <= 0:
            return self._neutral(context, latest_close, "Range di prezzo nullo")

        bin_width = price_range / bins
        volume_by_bin = self._build_profile(window, price_min, bin_width, bins)
        total_volume = sum(volume_by_bin)
        if total_volume <= 0:
            return self._neutral(context, latest_close, "Volume totale zero")

        poc_idx = max(range(bins), key=lambda i: volume_by_bin[i])
        poc_price = price_min + (poc_idx + 0.5) * bin_width
        poc_volume = volume_by_bin[poc_idx]

        # HVN: bin nel top hvn_percentile della distribuzione di volume
        sorted_vols = sorted(volume_by_bin, reverse=True)
        hvn_cutoff_index = max(0, int(bins * (1 - hvn_percentile)) - 1)
        hvn_threshold = sorted_vols[hvn_cutoff_index] if sorted_vols else 0
        poc_is_hvn = poc_volume >= hvn_threshold

        previous_close = candles[-2].close
        latest_close_val = candles[-1].close

        # Distanza dal POC in % per filtrare segnali "lontani"
        distance_pct = abs(latest_close_val - poc_price) / latest_close_val
        near_poc = distance_pct <= proximity_pct

        indicator_values = {
            "poc": round(poc_price, 6),
            "poc_volume": round(poc_volume, 2),
            "poc_is_hvn": poc_is_hvn,
            "distance_from_poc_pct": round(distance_pct * 100, 3),
            "bins": bins,
            "price_min": round(price_min, 6),
            "price_max": round(price_max, 6),
            "total_volume": round(total_volume, 2),
        }

        if not near_poc:
            return self._neutral(
                context,
                latest_close,
                f"Prezzo lontano dal POC ({distance_pct:.2%})",
                indicator_values,
            )

        # Reclaim/loss del POC: usa il previous close come riferimento del "venendo da..."
        reclaim_long = previous_close < poc_price and latest_close_val > poc_price
        loss_short = previous_close > poc_price and latest_close_val < poc_price

        if reclaim_long:
            confidence = 65 + (10 if poc_is_hvn else 0)
            confidence = min(80, confidence)
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.LONG,
                confidence=confidence,
                reason=f"POC reclaim long ({'HVN' if poc_is_hvn else 'standard'} at {poc_price:.6g})",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        if loss_short:
            confidence = 65 + (10 if poc_is_hvn else 0)
            confidence = min(80, confidence)
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.SHORT,
                confidence=confidence,
                reason=f"POC loss short ({'HVN' if poc_is_hvn else 'standard'} at {poc_price:.6g})",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        return self._neutral(
            context, latest_close, "Vicino al POC ma nessuna transizione", indicator_values
        )

    @staticmethod
    def _build_profile(
        candles: list[CandleInput],
        price_min: float,
        bin_width: float,
        bins: int,
    ) -> list[float]:
        """Distribuisce il volume di ogni candela uniformemente tra low e high.

        È l'approssimazione standard quando manca il tick-by-tick: invece di
        concentrare il volume sul close (sbagliato) o sull'OHLC4 (impreciso),
        spalmiamo proporzionalmente sui bin coperti dal range della candela.
        """
        profile = [0.0] * bins
        for candle in candles:
            candle_range = candle.high - candle.low
            if candle_range <= 0:
                # Candela "doji" con range nullo: tutto il volume in un singolo bin
                idx = min(bins - 1, max(0, int((candle.close - price_min) / bin_width)))
                profile[idx] += candle.volume
                continue
            low_bin = min(bins - 1, max(0, int((candle.low - price_min) / bin_width)))
            high_bin = min(bins - 1, max(0, int((candle.high - price_min) / bin_width)))
            n_bins = high_bin - low_bin + 1
            volume_per_bin = candle.volume / n_bins
            for i in range(low_bin, high_bin + 1):
                profile[i] += volume_per_bin
        return profile

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
