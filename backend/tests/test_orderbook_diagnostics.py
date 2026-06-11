from datetime import UTC, datetime

import pytest

from app.core.enums import Direction
from app.engine.orderbook import (
    analyze_order_book,
    ob_imbalance_result,
    reset_obi_stats,
    update_obi_stats,
)
from app.services.hyperliquid import L2BookSnapshot, OrderBookLevel
from app.strategies.base import CandleInput, StrategyContext


@pytest.fixture(autouse=True)
def clear_obi_stats():
    reset_obi_stats()
    yield
    reset_obi_stats()


def test_update_obi_stats_returns_zscore_against_previous_baseline() -> None:
    for value in [0.1, 0.2, 0.3, 0.4, 0.5]:
        update_obi_stats("BTC:10", value)

    stats = update_obi_stats("BTC:10", 0.6)

    assert stats.baseline_samples == 5
    assert stats.baseline_mean == pytest.approx(0.3)
    assert stats.baseline_std == pytest.approx(0.1414213562)
    assert stats.obi_delta == pytest.approx(0.3)
    assert stats.obi_zscore == pytest.approx(2.1213203436)


def test_update_obi_stats_handles_insufficient_or_flat_baseline() -> None:
    for value in [0.2, 0.2, 0.2, 0.2]:
        stats = update_obi_stats("ETH:10", value)
        assert stats.obi_zscore is None

    stats = update_obi_stats("ETH:10", 0.2)
    assert stats.baseline_samples == 4
    assert stats.obi_zscore is None

    stats = update_obi_stats("ETH:10", 0.4)
    assert stats.baseline_samples == 5
    assert stats.baseline_std == 0
    assert stats.obi_delta == pytest.approx(0.2)
    assert stats.obi_zscore is None


def test_ob_imbalance_result_behavior_is_unchanged() -> None:
    snapshot = L2BookSnapshot(
        bids=[
            OrderBookLevel(price=99, size=10),
            OrderBookLevel(price=98, size=10),
        ],
        asks=[
            OrderBookLevel(price=101, size=2),
            OrderBookLevel(price=102, size=2),
        ],
    )
    analysis = analyze_order_book(snapshot, entry=100, n_levels=2)
    context = StrategyContext(
        market="perp",
        symbol="BTC",
        market_type="perp",
        timeframe="5m",
        candles=[
            CandleInput(
                open_time=datetime(2026, 1, 1, tzinfo=UTC),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1000,
                open_interest=None,
            )
        ],
    )

    result = ob_imbalance_result(context, analysis)

    assert result is not None
    assert result.direction == Direction.LONG
    assert result.strategy_name == "ob_imbalance"
    assert result.indicator_values["source"] == "hyperliquid_l2"
