from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, floor
from typing import Protocol


class VolumeProfileCandle(Protocol):
    @property
    def open(self) -> float: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...

    @property
    def close(self) -> float: ...

    @property
    def volume(self) -> float: ...


@dataclass(frozen=True)
class VolumeProfileLevel:
    price: float
    low: float
    high: float
    buy_volume: float
    sell_volume: float

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def delta_volume(self) -> float:
        return self.buy_volume - self.sell_volume


@dataclass(frozen=True)
class VolumeProfileResult:
    levels: list[VolumeProfileLevel]
    poc_price: float
    poc_volume: float
    vah_price: float
    val_price: float
    value_area_volume: float
    value_area_pct: float
    total_volume: float
    delta_volume: float
    hvn_prices: list[float]
    lvn_prices: list[float]
    price_min: float
    price_max: float
    bin_width: float
    source: str
    fetched_at: float = 0.0

    @property
    def poc_is_hvn(self) -> bool:
        return self.poc_price in self.hvn_prices

    def compact_levels(self, limit: int = 12) -> list[dict[str, float]]:
        ranked = sorted(self.levels, key=lambda level: level.total_volume, reverse=True)
        return [
            {
                "price": round(level.price, 8),
                "volume": round(level.total_volume, 4),
                "buy_volume": round(level.buy_volume, 4),
                "sell_volume": round(level.sell_volume, 4),
                "delta_volume": round(level.delta_volume, 4),
            }
            for level in ranked[: max(0, limit)]
        ]


def compute_volume_profile(
    candles: Sequence[VolumeProfileCandle],
    *,
    bins: int = 30,
    value_area_pct: float = 0.70,
    hvn_percentile: float = 0.80,
    lvn_percentile: float = 0.20,
    tick_size: float | None = None,
    source: str = "ohlcv",
) -> VolumeProfileResult | None:
    if bins < 5 or not candles:
        return None
    price_min = min(float(candle.low) for candle in candles)
    price_max = max(float(candle.high) for candle in candles)
    if price_max <= price_min:
        return None

    aligned_min, aligned_max, bin_width, actual_bins = _profile_grid(
        price_min, price_max, bins, tick_size
    )
    buy_by_bin = [0.0] * actual_bins
    sell_by_bin = [0.0] * actual_bins

    for candle in candles:
        volume = max(0.0, float(candle.volume))
        if volume <= 0:
            continue
        low = float(candle.low)
        high = float(candle.high)
        close = float(candle.close)
        open_ = float(candle.open)
        low_bin = _bin_index(low, aligned_min, bin_width, actual_bins)
        high_bin = _bin_index(high, aligned_min, bin_width, actual_bins)
        bin_count = high_bin - low_bin + 1
        volume_per_bin = volume / bin_count
        if close > open_:
            buy_share = 1.0
        elif close < open_:
            buy_share = 0.0
        else:
            buy_share = 0.5
        for index in range(low_bin, high_bin + 1):
            buy_by_bin[index] += volume_per_bin * buy_share
            sell_by_bin[index] += volume_per_bin * (1.0 - buy_share)

    levels = [
        VolumeProfileLevel(
            price=aligned_min + (index + 0.5) * bin_width,
            low=aligned_min + index * bin_width,
            high=aligned_min + (index + 1) * bin_width,
            buy_volume=buy_by_bin[index],
            sell_volume=sell_by_bin[index],
        )
        for index in range(actual_bins)
    ]
    return build_volume_profile_from_levels(
        levels,
        value_area_pct=value_area_pct,
        hvn_percentile=hvn_percentile,
        lvn_percentile=lvn_percentile,
        source=source,
    )


def build_volume_profile_from_triplets(
    triplets: list[tuple[float, float, float]],
    *,
    value_area_pct: float = 0.70,
    hvn_percentile: float = 0.80,
    lvn_percentile: float = 0.20,
    source: str = "kiotaka",
) -> VolumeProfileResult | None:
    if not triplets:
        return None
    sorted_triplets = sorted(triplets, key=lambda item: item[0])
    if len(sorted_triplets) > 1:
        gaps = [
            sorted_triplets[index + 1][0] - sorted_triplets[index][0]
            for index in range(len(sorted_triplets) - 1)
            if sorted_triplets[index + 1][0] > sorted_triplets[index][0]
        ]
        bin_width = min(gaps) if gaps else max(abs(sorted_triplets[0][0]) * 0.0001, 1e-8)
    else:
        bin_width = max(abs(sorted_triplets[0][0]) * 0.0001, 1e-8)

    levels = [
        VolumeProfileLevel(
            price=price,
            low=price - bin_width / 2,
            high=price + bin_width / 2,
            buy_volume=max(0.0, buy_volume),
            sell_volume=max(0.0, sell_volume),
        )
        for price, buy_volume, sell_volume in sorted_triplets
    ]
    return build_volume_profile_from_levels(
        levels,
        value_area_pct=value_area_pct,
        hvn_percentile=hvn_percentile,
        lvn_percentile=lvn_percentile,
        source=source,
    )


def build_volume_profile_from_levels(
    levels: list[VolumeProfileLevel],
    *,
    value_area_pct: float = 0.70,
    hvn_percentile: float = 0.80,
    lvn_percentile: float = 0.20,
    source: str,
) -> VolumeProfileResult | None:
    if not levels:
        return None
    total_volume = sum(level.total_volume for level in levels)
    if total_volume <= 0:
        return None

    poc_index = max(range(len(levels)), key=lambda index: levels[index].total_volume)
    poc = levels[poc_index]
    value_area_indices, value_area_volume = _value_area(levels, poc_index, value_area_pct)
    if value_area_indices:
        val_price = levels[min(value_area_indices)].price
        vah_price = levels[max(value_area_indices)].price
    else:
        val_price = vah_price = poc.price

    volumes = [level.total_volume for level in levels]
    hvn_threshold = _percentile(volumes, hvn_percentile)
    lvn_threshold = _percentile(volumes, lvn_percentile)
    hvn_prices = [level.price for level in levels if level.total_volume >= hvn_threshold]
    lvn_prices = [
        level.price
        for level in levels
        if level.total_volume > 0 and level.total_volume <= lvn_threshold
    ]
    bin_width = levels[0].high - levels[0].low
    return VolumeProfileResult(
        levels=levels,
        poc_price=poc.price,
        poc_volume=poc.total_volume,
        vah_price=vah_price,
        val_price=val_price,
        value_area_volume=value_area_volume,
        value_area_pct=value_area_pct,
        total_volume=total_volume,
        delta_volume=sum(level.delta_volume for level in levels),
        hvn_prices=hvn_prices,
        lvn_prices=lvn_prices,
        price_min=min(level.low for level in levels),
        price_max=max(level.high for level in levels),
        bin_width=bin_width,
        source=source,
    )


def _profile_grid(
    price_min: float,
    price_max: float,
    bins: int,
    tick_size: float | None,
) -> tuple[float, float, float, int]:
    if tick_size is None or tick_size <= 0:
        bin_width = (price_max - price_min) / bins
        return price_min, price_max, bin_width, bins

    aligned_min = floor(price_min / tick_size) * tick_size
    aligned_max = ceil(price_max / tick_size) * tick_size
    total_ticks = max(1, ceil((aligned_max - aligned_min) / tick_size))
    raw_ticks_per_bin = max(1.0, total_ticks / bins)
    lower = max(1, floor(raw_ticks_per_bin))
    upper = max(1, ceil(raw_ticks_per_bin))
    lower_bins = ceil(total_ticks / lower)
    upper_bins = ceil(total_ticks / upper)
    ticks_per_bin = lower if abs(lower_bins - bins) <= abs(upper_bins - bins) else upper
    actual_bins = ceil(total_ticks / ticks_per_bin)
    bin_width = ticks_per_bin * tick_size
    return aligned_min, aligned_min + actual_bins * bin_width, bin_width, actual_bins


def _bin_index(price: float, price_min: float, bin_width: float, bins: int) -> int:
    return min(bins - 1, max(0, int((price - price_min) / bin_width)))


def _value_area(
    levels: list[VolumeProfileLevel],
    poc_index: int,
    value_area_pct: float,
) -> tuple[set[int], float]:
    target = sum(level.total_volume for level in levels) * min(max(value_area_pct, 0.0), 1.0)
    included = {poc_index}
    area_volume = levels[poc_index].total_volume
    below = poc_index - 1
    above = poc_index + 1
    while area_volume < target and (below >= 0 or above < len(levels)):
        below_volume = levels[below].total_volume if below >= 0 else -1.0
        above_volume = levels[above].total_volume if above < len(levels) else -1.0
        if above_volume >= below_volume:
            included.add(above)
            area_volume += max(0.0, above_volume)
            above += 1
        else:
            included.add(below)
            area_volume += max(0.0, below_volume)
            below -= 1
    return included, area_volume


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    pct = min(max(percentile, 0.0), 1.0)
    sorted_values = sorted(values)
    index = min(len(sorted_values) - 1, max(0, ceil(len(sorted_values) * pct) - 1))
    return sorted_values[index]
