from __future__ import annotations

from app.core.enums import Direction
from app.strategies.base import StrategyContext, StrategyResult


class CvdDivergenceStrategy:
    """Cumulative Volume Delta — approssimato via segno del body della candela.

    Limite noto: senza il trade-tape (lato aggressivo dei trade), il CVD "vero"
    non è ricavabile da sole OHLCV. Approssimiamo con `sign(close-open) * volume`
    — è la convenzione "Bull-Bear Volume Delta" usata da molti tool retail. Il
    risultato è correlato col CVD vero ma rumoroso su candele indecise (doji).

    Logica del segnale: divergenza tra prezzo e CVD su una finestra di lookback.
    - Bullish: il prezzo fa un nuovo MINIMO ma il CVD fa un minimo PIÙ ALTO
      → c'è meno pressione di vendita aggressiva, possibile reversal LONG
    - Bearish: il prezzo fa un nuovo MASSIMO ma il CVD fa un massimo PIÙ BASSO
      → c'è meno pressione di acquisto aggressiva, possibile reversal SHORT
    """

    name = "cvd_divergence"

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        lookback = int(params.get("lookback", 30))
        min_divergence_pct = float(params.get("min_divergence_pct", 0.005))
        candles = context.candles
        latest_close = candles[-1].close if candles else None

        if len(candles) < lookback + 2:
            return self._neutral(context, latest_close, "Dati CVD insufficienti")

        window = candles[-lookback:]
        # CVD approssimato: somma cumulativa di sign(close-open) × volume
        cvd_series: list[float] = []
        running = 0.0
        for candle in window:
            sign = 0.0
            if candle.close > candle.open:
                sign = 1.0
            elif candle.close < candle.open:
                sign = -1.0
            running += sign * candle.volume
            cvd_series.append(running)

        # Trova min/max prezzo e CVD nella prima metà vs seconda metà della finestra
        # per detectare divergenze "swing-based" (più robuste vs micro-oscillazioni)
        half = lookback // 2
        first_half_lows = [c.low for c in window[:half]]
        first_half_highs = [c.high for c in window[:half]]
        second_half_lows = [c.low for c in window[half:]]
        second_half_highs = [c.high for c in window[half:]]

        price_low_first = min(first_half_lows)
        price_low_second = min(second_half_lows)
        price_high_first = max(first_half_highs)
        price_high_second = max(second_half_highs)

        cvd_first_half = cvd_series[:half]
        cvd_second_half = cvd_series[half:]
        cvd_low_first = min(cvd_first_half)
        cvd_low_second = min(cvd_second_half)
        cvd_high_first = max(cvd_first_half)
        cvd_high_second = max(cvd_second_half)

        # Soglia minima di divergenza in % per evitare segnali su movimenti irrilevanti
        threshold = price_low_first * min_divergence_pct

        bullish_div = (
            price_low_second < price_low_first - threshold
            and cvd_low_second > cvd_low_first
        )
        bearish_div = (
            price_high_second > price_high_first + threshold
            and cvd_high_second < cvd_high_first
        )

        indicator_values = {
            "cvd_latest": cvd_series[-1],
            "cvd_low_first": cvd_low_first,
            "cvd_low_second": cvd_low_second,
            "cvd_high_first": cvd_high_first,
            "cvd_high_second": cvd_high_second,
            "price_low_first": price_low_first,
            "price_low_second": price_low_second,
            "price_high_first": price_high_first,
            "price_high_second": price_high_second,
        }

        if bullish_div and bearish_div:
            # Caso patologico (very rare): segnali contrastanti → neutralizza
            return self._neutral(
                context, latest_close, "Divergenza CVD ambigua (long+short)", indicator_values
            )

        if bullish_div:
            # Intensità: quanto è ampia la differenza relativa
            cvd_delta = (cvd_low_second - cvd_low_first) / max(abs(cvd_low_first), 1.0)
            confidence = min(78, 55 + int(abs(cvd_delta) * 200))
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.LONG,
                confidence=confidence,
                reason="CVD bullish divergence: prezzo a nuovi minimi, CVD risale",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        if bearish_div:
            cvd_delta = (cvd_high_first - cvd_high_second) / max(abs(cvd_high_first), 1.0)
            confidence = min(78, 55 + int(abs(cvd_delta) * 200))
            return StrategyResult(
                strategy_name=self.name,
                market=context.market_type,
                symbol=context.symbol,
                timeframe=context.timeframe,
                direction=Direction.SHORT,
                confidence=confidence,
                reason="CVD bearish divergence: prezzo a nuovi massimi, CVD scende",
                indicator_values=indicator_values,
                suggested_entry=latest_close,
            )

        return self._neutral(context, latest_close, "Nessuna divergenza CVD", indicator_values)

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
