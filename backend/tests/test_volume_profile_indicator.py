from __future__ import annotations

from datetime import UTC, datetime

from app.indicators.volume_profile import (
    build_volume_profile_from_triplets,
    compute_volume_profile,
)
from app.strategies.base import CandleInput


def _candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> CandleInput:
    return CandleInput(
        open_time=datetime(2026, 1, 1, tzinfo=UTC),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_triplet_profile_computes_poc_value_area_and_nodes() -> None:
    profile = build_volume_profile_from_triplets(
        [
            (98.0, 10.0, 0.0),
            (99.0, 20.0, 0.0),
            (100.0, 60.0, 10.0),
            (101.0, 30.0, 0.0),
            (102.0, 10.0, 0.0),
        ],
        value_area_pct=0.70,
        hvn_percentile=0.80,
        lvn_percentile=0.20,
    )

    assert profile is not None
    assert profile.poc_price == 100.0
    assert profile.poc_volume == 70.0
    assert profile.val_price == 100.0
    assert profile.vah_price == 101.0
    assert profile.total_volume == 140.0
    assert profile.delta_volume == 120.0
    assert 100.0 in profile.hvn_prices
    assert 98.0 in profile.lvn_prices


def test_ohlcv_profile_returns_none_for_zero_volume_or_flat_range() -> None:
    assert compute_volume_profile([_candle(100, 101, 99, 100, 0)]) is None
    assert compute_volume_profile([_candle(100, 100, 100, 100, 10)]) is None


def test_ohlcv_profile_tick_rounding_uses_whole_tick_bins() -> None:
    candles = [
        _candle(10.10, 10.31, 10.01, 10.25, 10),
        _candle(10.50, 10.99, 10.45, 10.48, 20),
    ]
    profile = compute_volume_profile(candles, bins=30, tick_size=0.01)

    assert profile is not None
    ticks = profile.bin_width / 0.01
    assert abs(ticks - round(ticks)) < 1e-9
    assert profile.price_min <= 10.01
    assert profile.price_max >= 10.99
