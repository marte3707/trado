from datetime import UTC, datetime

from app.core.enums import Direction
from app.engine.mtf import check_mtf_confluence, higher_timeframe
from app.strategies.base import CandleInput


def _candles(prices: list[float]) -> list[CandleInput]:
    return [
        CandleInput(
            datetime.now(UTC),
            price - 0.1,
            price + 0.3,
            price - 0.3,
            price,
            1000,
        )
        for price in prices
    ]


def test_higher_timeframe_mapping() -> None:
    assert higher_timeframe("15m") == "1h"
    assert higher_timeframe("1h") == "4h"
    assert higher_timeframe("unknown") is None


def test_mtf_confluence_disabled_passes() -> None:
    candles = _candles([100.0] * 60)
    passed, info = check_mtf_confluence(candles, Direction.LONG, enabled=False)
    assert passed is True
    assert info is None


def test_mtf_confluence_long_passes_when_higher_trend_is_up() -> None:
    candles = _candles([100.0 + i * 0.5 for i in range(80)])
    passed, info = check_mtf_confluence(candles, Direction.LONG, enabled=True, ema_period=50)
    assert passed is True
    assert info is not None
    assert info["higher_trend"] == "up"


def test_mtf_confluence_long_blocked_when_higher_trend_is_down() -> None:
    candles = _candles([200.0 - i * 0.5 for i in range(80)])
    passed, info = check_mtf_confluence(candles, Direction.LONG, enabled=True, ema_period=50)
    assert passed is False
    assert info is not None
    assert info["higher_trend"] == "down"


def test_mtf_confluence_short_blocked_when_higher_trend_is_up() -> None:
    candles = _candles([100.0 + i * 0.5 for i in range(80)])
    passed, info = check_mtf_confluence(candles, Direction.SHORT, enabled=True, ema_period=50)
    assert passed is False
    assert info is not None
    assert info["higher_trend"] == "up"


def test_mtf_confluence_too_few_candles_passes() -> None:
    candles = _candles([100.0] * 10)
    passed, info = check_mtf_confluence(candles, Direction.LONG, enabled=True, ema_period=50)
    assert passed is True
    assert info is None


def test_mtf_confluence_neutral_direction_passes() -> None:
    candles = _candles([100.0] * 60)
    passed, info = check_mtf_confluence(candles, Direction.NEUTRAL, enabled=True)
    assert passed is True
    assert info is None
