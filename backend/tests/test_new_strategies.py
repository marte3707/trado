"""Test per le strategie nuove §4.8: CVD, Liquidation Cascade, Volume Profile."""

from datetime import datetime, timedelta, timezone

from app.core.enums import Direction
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.cvd_divergence_strategy import CvdDivergenceStrategy
from app.strategies.liquidation_cascade_strategy import LiquidationCascadeStrategy
from app.strategies.volume_profile_strategy import VolumeProfileStrategy

BASE_TIME = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> CandleInput:
    return CandleInput(
        open_time=BASE_TIME + timedelta(minutes=i * 5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _make_context(candles: list[CandleInput]) -> StrategyContext:
    return StrategyContext(
        market="hyperliquid",
        symbol="BTC",
        timeframe="5m",
        market_type="perpetual",
        candles=candles,
    )


# ---------------------------------------------------------------------------
# CVD divergence
# ---------------------------------------------------------------------------


def test_cvd_divergence_neutral_when_data_insufficient() -> None:
    candles = [_candle(i, 100, 101, 99, 100, 1.0) for i in range(5)]
    result = CvdDivergenceStrategy().evaluate(_make_context(candles), {})

    assert result.direction == Direction.NEUTRAL
    assert "insufficienti" in result.reason.lower()


def test_cvd_bullish_divergence_detected() -> None:
    """Prezzo fa nuovo minimo nella seconda metà, ma il CVD risale
    perché ci sono più candele bullish con volume nella seconda metà.

    Costruzione:
    - Prima metà (15 candle): tutte bearish, CVD scende → cvd_low_first ≈ -75
    - Seconda metà (15 candle): prima 8 bullish con vol alto (CVD risale a > -75),
      poi 1 candle con dip di prezzo (nuovo low), poi 6 bullish.
      Risultato: cvd_low_second > cvd_low_first MA price_low_second < price_low_first
    """
    candles: list[CandleInput] = []
    # Padding iniziale (la strategia richiede len(candles) >= lookback + 2)
    for i in range(5):
        candles.append(_candle(i, 100.0, 100.1, 99.9, 100.0, 5.0))
    # Prima metà del lookback: 15 candele bearish, vol 5 ciascuna → CVD scende a -75
    price = 100.0
    for i in range(15):
        op = price
        cl = price - 0.5
        candles.append(_candle(5 + i, op, op + 0.05, cl - 0.05, cl, 5.0))
        price = cl
    # price ora ≈ 92.5; prima metà price_low ≈ 92.45

    # Seconda metà: prima 8 candele bullish vol 20 → CVD risale a +85 (min ≈ -75 + 20 = -55)
    for j in range(8):
        op = price
        cl = price + 0.5
        candles.append(_candle(20 + j, op, cl + 0.05, op - 0.05, cl, 20.0))
        price = cl
    # price ≈ 96.5

    # Una candela con nuovo low di PREZZO (sotto 92.45) ma vol basso e bullish-close →
    # CVD non scende sotto -55
    candles.append(_candle(28, price, price + 0.05, 92.0, price + 0.05, 5.0))
    # price low della candela = 92.0 (nuovo minimo!), close = price + 0.05 (bullish, CVD +5)
    price = price + 0.05

    # Altre 6 candele bullish per chiudere la finestra
    for j in range(6):
        op = price
        cl = price + 0.3
        candles.append(_candle(29 + j, op, cl + 0.05, op - 0.05, cl, 15.0))
        price = cl

    result = CvdDivergenceStrategy().evaluate(
        _make_context(candles), {"lookback": 30, "min_divergence_pct": 0.001}
    )

    assert result.direction == Direction.LONG
    assert "bullish" in result.reason.lower()


def test_cvd_no_divergence_in_clean_uptrend() -> None:
    candles = []
    for i in range(40):
        open_p = 100.0 + i * 0.3
        close_p = open_p + 0.2
        high_p = close_p + 0.05
        low_p = open_p - 0.05
        candles.append(_candle(i, open_p, high_p, low_p, close_p, 10.0))

    result = CvdDivergenceStrategy().evaluate(_make_context(candles), {"lookback": 30})
    assert result.direction == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# Liquidation Cascade
# ---------------------------------------------------------------------------


def test_liquidation_cascade_neutral_when_insufficient_history() -> None:
    candles = [_candle(i, 100, 101, 99, 100, 1.0) for i in range(20)]
    result = LiquidationCascadeStrategy().evaluate(_make_context(candles), {})
    assert result.direction == Direction.NEUTRAL


def test_liquidation_cascade_long_spike_down_absorbed() -> None:
    """Pattern long-liquidation: 60 candele "normal", poi un candle con spike
    down enorme e wick basso lungo ma close in alto = liquidazione long completata."""
    candles: list[CandleInput] = []
    for i in range(70):
        open_p = 100.0 + (i % 5) * 0.1
        close_p = open_p + 0.05
        high_p = close_p + 0.2
        low_p = open_p - 0.2
        candles.append(_candle(i, open_p, high_p, low_p, close_p, 5.0))

    # Candela cascata: open 100, dump a 95 (wick basso), recupera a 99.5 (close alto)
    candles.append(_candle(70, 100.0, 100.2, 95.0, 99.5, 25.0))

    result = LiquidationCascadeStrategy().evaluate(
        _make_context(candles),
        {
            "lookback": 50,
            "volume_factor": 2.0,
            "range_factor": 1.5,
            "wick_min_ratio": 0.5,
            "close_position_threshold": 0.40,
        },
    )

    assert result.direction == Direction.LONG
    assert "absorbed" in result.reason.lower()


def test_liquidation_cascade_short_spike_up_absorbed() -> None:
    candles: list[CandleInput] = []
    for i in range(70):
        open_p = 100.0 + (i % 5) * 0.1
        close_p = open_p + 0.05
        high_p = close_p + 0.2
        low_p = open_p - 0.2
        candles.append(_candle(i, open_p, high_p, low_p, close_p, 5.0))

    # Candela cascata: open 100, pump a 105 (wick alto), respinto a 100.5
    candles.append(_candle(70, 100.0, 105.0, 99.8, 100.5, 25.0))

    result = LiquidationCascadeStrategy().evaluate(
        _make_context(candles),
        {
            "lookback": 50,
            "volume_factor": 2.0,
            "range_factor": 1.5,
            "wick_min_ratio": 0.5,
            "close_position_threshold": 0.40,
        },
    )

    assert result.direction == Direction.SHORT
    assert "absorbed" in result.reason.lower()


def test_liquidation_cascade_ignores_high_volume_without_wick() -> None:
    """Volume alto ma candle "drittato" (no wick di assorbimento) → neutral."""
    candles: list[CandleInput] = []
    for i in range(70):
        candles.append(_candle(i, 100, 100.1, 99.9, 100, 5.0))
    # Candle con volume alto ma trend, no wick lungo
    candles.append(_candle(70, 100, 105, 99.9, 104.9, 30.0))

    result = LiquidationCascadeStrategy().evaluate(
        _make_context(candles),
        {"lookback": 50, "volume_factor": 2.0, "range_factor": 1.5},
    )
    assert result.direction == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# Volume Profile
# ---------------------------------------------------------------------------


def test_volume_profile_neutral_when_data_insufficient() -> None:
    candles = [_candle(i, 100, 101, 99, 100, 1.0) for i in range(50)]
    result = VolumeProfileStrategy().evaluate(_make_context(candles), {"lookback": 100})
    assert result.direction == Direction.NEUTRAL


def _profile_candles_with_poc_at_100() -> list[CandleInput]:
    """Helper: costruisce 100 candele dove il POC è chiaramente a price=100.

    Usa candele "strette" (high-low piccolo) per concentrare il volume in un
    unico bin. Le candele "fuori POC" sono distribuite tra 98 e 102 con bassa
    densità di volume.
    """
    candles: list[CandleInput] = []
    for i in range(100):
        if i % 5 == 2:
            # Candela al POC: range strettissimo (1 bin), volume alto
            candles.append(_candle(i, 100.0, 100.05, 99.95, 100.0, 100.0))
        else:
            # Candele fuori POC: range stretto, prezzi vari, volume basso
            price = 98.0 + (i % 5)
            candles.append(_candle(i, price, price + 0.05, price - 0.05, price, 3.0))
    return candles


def test_volume_profile_reclaim_long_at_poc() -> None:
    """Volume concentrato a 100 (POC), prezzo viene da sotto e supera 100."""
    candles = _profile_candles_with_poc_at_100()
    # Penultima: close = 99.5 (sotto POC). Ultima: close = 100.3 (riprende POC)
    candles.append(_candle(100, 99.5, 99.7, 99.3, 99.5, 5.0))
    candles.append(_candle(101, 99.6, 100.4, 99.5, 100.3, 5.0))

    result = VolumeProfileStrategy().evaluate(
        _make_context(candles),
        {"lookback": 100, "bins": 20, "proximity_pct": 0.01},
    )

    assert result.direction == Direction.LONG
    assert "POC reclaim" in result.reason


def test_volume_profile_loss_short_at_poc() -> None:
    candles = _profile_candles_with_poc_at_100()
    # Penultima sopra il POC, ultima sotto
    candles.append(_candle(100, 100.5, 100.8, 100.3, 100.5, 5.0))
    candles.append(_candle(101, 100.4, 100.6, 99.5, 99.7, 5.0))

    result = VolumeProfileStrategy().evaluate(
        _make_context(candles),
        {"lookback": 100, "bins": 20, "proximity_pct": 0.01},
    )

    assert result.direction == Direction.SHORT
    assert "POC loss" in result.reason


def test_volume_profile_neutral_when_far_from_poc() -> None:
    candles = _profile_candles_with_poc_at_100()
    # Prezzo molto lontano dal POC (a ~95, mentre POC ≈ 100)
    candles.append(_candle(100, 95.0, 95.1, 94.9, 95.0, 5.0))
    candles.append(_candle(101, 95.0, 95.2, 94.8, 95.1, 5.0))

    result = VolumeProfileStrategy().evaluate(
        _make_context(candles),
        {"lookback": 100, "bins": 20, "proximity_pct": 0.005},
    )

    assert result.direction == Direction.NEUTRAL
    assert "lontano dal POC" in result.reason
