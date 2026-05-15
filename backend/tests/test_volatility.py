from datetime import datetime, timezone

from app.engine.volatility import atr_percentile_rank, evaluate_volatility
from app.strategies.base import CandleInput


def _candles(prices: list[float]) -> list[CandleInput]:
    return [
        CandleInput(
            datetime.now(timezone.utc),
            price - 0.1,
            price + 0.4,
            price - 0.4,
            price,
            1000,
        )
        for price in prices
    ]


def test_atr_percentile_rank_returns_none_when_too_few_values() -> None:
    assert atr_percentile_rank([None] * 10) is None
    assert atr_percentile_rank([1.0, 2.0, 3.0]) is None


def test_atr_percentile_rank_high_when_current_is_max() -> None:
    series = [float(i) for i in range(1, 60)]
    rank = atr_percentile_rank(series)
    assert rank is not None
    assert rank == 1.0


def test_evaluate_volatility_disabled_passes_through() -> None:
    candles = _candles([100.0 + i * 0.5 for i in range(80)])
    passed, info = evaluate_volatility(candles, enabled=False)
    assert passed is True
    assert info is None


def _variable_atr_candles(count: int = 80) -> list[CandleInput]:
    # candele in cui le ultime hanno range molto più piccolo → ATR finale basso.
    candles: list[CandleInput] = []
    for i in range(count):
        close = 100.0 + i * 0.2
        range_pct = 0.04 if i < count - 5 else 0.0005
        candles.append(
            CandleInput(
                datetime.now(timezone.utc),
                close - range_pct * 0.4,
                close + range_pct * 0.5,
                close - range_pct * 0.5,
                close,
                1000,
            )
        )
    return candles


def test_evaluate_volatility_blocks_when_below_min_percentile() -> None:
    candles = _variable_atr_candles()
    passed, info = evaluate_volatility(candles, enabled=True, pct_min=0.30, pct_max=1.0)
    assert info is not None
    assert info["passed"] is False
    assert passed is False


def test_evaluate_volatility_passes_when_within_range() -> None:
    candles = _candles([100.0 + i * 0.5 for i in range(80)])
    passed, info = evaluate_volatility(candles, enabled=True, pct_min=0.05, pct_max=1.0)
    assert passed is True
    assert info is not None
    assert info["passed"] is True
