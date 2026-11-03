"""Read-only fixed-horizon screen of pre-gate 4h paper observations.

This screen measures directional price movement after costs. It enters at the
first 15m candle open after the recorded decision, never at an earlier 4h open.
It is not a position backtest, does not include stops or slippage, and cannot
establish a tradable edge or authorize live promotion. Train and holdout are
fixed in source-candle order, with disjoint windows and a horizon embargo.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db.models import Candle, DecisionLog
from app.db.session import SessionLocal

HORIZONS = (1, 3, 6)
ORIENTATIONS = ("follow", "fade")
BAR_DURATION = timedelta(hours=4)
ENTRY_BAR_DURATION = timedelta(minutes=15)
TRAIN_COUNT = 40
HOLDOUT_COUNT = 20
TRAIN_MIN_SOURCE_CANDLES = 20
HOLDOUT_MIN_SOURCE_CANDLES = 10
MIN_MARKETS_PER_PERIOD = 5
MIN_PROFIT_FACTOR = Decimal("1.20")
ZERO = Decimal("0")


@dataclass(frozen=True)
class ShadowObservation:
    source_open: datetime
    market_id: int
    net_return: Decimal | None
    data_status: str


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def score_band(score: int) -> str:
    if score < 50:
        return "00-49"
    if score >= 80:
        return "80-100"
    lower = (score // 10) * 10
    return f"{lower:02d}-{lower + 9:02d}"


def first_post_decision_entry(
    decision_at: datetime,
    bars: list[Candle],
) -> Candle | None:
    """Use the next 15m open only when its timestamp follows the decision."""
    eligible = [
        bar for bar in bars
        if bar.timeframe == "15m"
        and decision_at < bar.open_time <= decision_at + ENTRY_BAR_DURATION
        and bar.open > ZERO
    ]
    return min(eligible, key=lambda bar: bar.open_time) if eligible else None


def fixed_horizon_net_return(
    source_open: datetime,
    bars: list[Candle],
    *,
    entry: Candle | None,
    direction: str,
    horizon: int,
    taker_fee_rate: Decimal,
    as_of: datetime | None = None,
) -> Decimal | None:
    """Enter at the next bar open, leave at horizon close, after two fees."""
    if (
        direction not in {"LONG", "SHORT"}
        or horizon < 1
        or taker_fee_rate < ZERO
        or entry is None
        or entry.open <= ZERO
    ):
        return None
    by_open = {bar.open_time: bar for bar in bars}
    expected = [source_open + BAR_DURATION * index for index in range(1, horizon + 1)]
    if expected[-1] + BAR_DURATION > (as_of or datetime.now(UTC)):
        return None
    if entry.open_time >= expected[-1] + BAR_DURATION:
        return None
    if any(open_time not in by_open for open_time in expected):
        return None
    exit_price = by_open[expected[-1]].close
    if exit_price <= ZERO:
        return None
    exit_notional_ratio = exit_price / entry.open
    signed = exit_notional_ratio - 1
    if direction == "SHORT":
        signed = -signed
    # The exit fee is charged on exit notional, not entry notional.
    return signed - taker_fee_rate * (1 + exit_notional_ratio)


def _metrics(values: list[Decimal]) -> dict[str, Any]:
    gains = sum((value for value in values if value > ZERO), ZERO)
    losses = -sum((value for value in values if value < ZERO), ZERO)
    total = sum(values, ZERO)
    profit_factor = gains / losses if losses > ZERO else None
    return {
        "n": len(values),
        "net_return_sum": str(total),
        "average_net_return": str(total / len(values)) if values else None,
        "profit_factor": str(profit_factor) if profit_factor is not None else None,
        "criteria_met": bool(values)
        and total > ZERO
        and (profit_factor is None and gains > ZERO or profit_factor is not None and profit_factor >= MIN_PROFIT_FACTOR),
    }


def _take_period(
    observations: list[ShadowObservation],
    *,
    min_entries: int,
    min_source_candles: int,
) -> tuple[list[ShadowObservation], int, bool]:
    """Keep whole source-candle groups; never use outcomes to set a boundary."""
    taken: list[ShadowObservation] = []
    sources: set[datetime] = set()
    markets: set[int] = set()
    cursor = 0
    while cursor < len(observations):
        source = observations[cursor].source_open
        while cursor < len(observations) and observations[cursor].source_open == source:
            observation = observations[cursor]
            taken.append(observation)
            markets.add(observation.market_id)
            cursor += 1
        sources.add(source)
        if (
            len(taken) >= min_entries
            and len(sources) >= min_source_candles
            and len(markets) >= MIN_MARKETS_PER_PERIOD
        ):
            return taken, cursor, True
    return taken, cursor, False


def _period_metrics(observations: list[ShadowObservation]) -> dict[str, Any]:
    available = [row.net_return for row in observations if row.net_return is not None]
    result = _metrics(available)
    result["criteria_met"] = result["criteria_met"] and len(available) == len(observations)
    result.update({
        "entries": len(observations),
        "source_candles": len({row.source_open for row in observations}),
        "markets": len({row.market_id for row in observations}),
        "pending": sum(row.data_status == "pending" for row in observations),
        "missing_market_data": sum(row.data_status == "missing" for row in observations),
    })
    return result


def split_fixed_cohort(
    observations: list[ShadowObservation],
    *,
    horizon: int,
) -> tuple[list[ShadowObservation], list[ShadowObservation], datetime | None, bool]:
    """Fix train before holdout and embargo their overlapping return windows."""
    ordered = sorted(observations, key=lambda item: (item.source_open, item.market_id))
    train_rows, cursor, train_ready = _take_period(
        ordered, min_entries=TRAIN_COUNT,
        min_source_candles=TRAIN_MIN_SOURCE_CANDLES,
    )
    embargo_until = (
        train_rows[-1].source_open + BAR_DURATION * (horizon + 1)
        if train_ready else None
    )
    holdout_pool = (
        [row for row in ordered[cursor:] if row.source_open > embargo_until]
        if embargo_until is not None else []
    )
    holdout_rows, _, holdout_ready = _take_period(
        holdout_pool, min_entries=HOLDOUT_COUNT,
        min_source_candles=HOLDOUT_MIN_SOURCE_CANDLES,
    )
    return train_rows, holdout_rows, embargo_until, train_ready and holdout_ready


def shadow_report(db) -> dict[str, Any]:
    logs = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "candidate_shadow_observation")
        .order_by(DecisionLog.timestamp, DecisionLog.id)
        .all()
    )
    seen: set[tuple[int, datetime, str]] = set()
    grouped: dict[tuple[str, str, str, str, int], list[ShadowObservation]] = defaultdict(list)
    duplicates = 0
    incomplete = 0
    as_of = datetime.now(UTC)
    for log in logs:
        data = log.data if isinstance(log.data, dict) else {}
        if log.market_id is None or data.get("timeframe") != "4h":
            continue
        try:
            source_open = datetime.fromisoformat(str(data["source_candle_open_time"]))
            score = int(data["score"])
        except (KeyError, TypeError, ValueError):
            incomplete += 1
            continue
        if source_open.tzinfo is None:
            source_open = source_open.replace(tzinfo=UTC)
        direction = str(data.get("direction") or "")
        config_hash = str(data.get("configuration_hash") or "")
        fee = _decimal(data.get("taker_fee_rate"))
        if direction not in {"LONG", "SHORT"} or not config_hash or fee is None:
            continue
        identity = (log.market_id, source_open, config_hash)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        decision_at = log.timestamp
        if decision_at.tzinfo is None:
            decision_at = decision_at.replace(tzinfo=UTC)
        entry_bars = (
            db.query(Candle)
            .filter(
                Candle.market_id == log.market_id,
                Candle.timeframe == "15m",
                Candle.open_time > decision_at,
                Candle.open_time <= decision_at + ENTRY_BAR_DURATION,
            )
            .order_by(Candle.open_time)
            .all()
        )
        entry = first_post_decision_entry(decision_at, entry_bars)
        bars = (
            db.query(Candle)
            .filter(
                Candle.market_id == log.market_id,
                Candle.timeframe == "4h",
                Candle.open_time >= source_open + BAR_DURATION,
                Candle.open_time <= source_open + BAR_DURATION * max(HORIZONS),
            )
            .order_by(Candle.open_time)
            .all()
        )
        cohorts = [f"score:{score_band(score)}"]
        aligned = data.get("aligned_strategies")
        if direction == "LONG" and isinstance(aligned, list):
            cohorts.append(
                "wyckoff_long:aligned"
                if "wyckoff_spring_upthrust" in aligned
                else "wyckoff_long:other"
            )
        for orientation in ORIENTATIONS:
            evaluated_direction = (
                direction
                if orientation == "follow"
                else "SHORT" if direction == "LONG" else "LONG"
            )
            for horizon in HORIZONS:
                value = fixed_horizon_net_return(
                    source_open, bars, entry=entry,
                    direction=evaluated_direction, horizon=horizon,
                    taker_fee_rate=fee, as_of=as_of,
                )
                data_status = (
                    "ready" if value is not None
                    else "pending" if source_open + BAR_DURATION * (horizon + 1) > as_of
                    else "missing"
                )
                for cohort in cohorts:
                    grouped[(config_hash, direction, cohort, orientation, horizon)].append(
                        ShadowObservation(source_open, log.market_id, value, data_status)
                    )
    cells = []
    for (config_hash, direction, cohort, orientation, horizon), observations in sorted(grouped.items()):
        train_rows, holdout_rows, embargo_until, ready = split_fixed_cohort(
            observations, horizon=horizon,
        )
        train = _period_metrics(train_rows)
        holdout = _period_metrics(holdout_rows)
        selected = train_rows + holdout_rows
        cells.append({
            "configuration_hash": config_hash,
            "direction": direction,
            "cohort": cohort,
            "score_band": cohort.removeprefix("score:") if cohort.startswith("score:") else None,
            "orientation": orientation,
            "horizon_bars": horizon,
            "observations": len(observations),
            "embargo_until": embargo_until.isoformat() if embargo_until else None,
            "status": (
                "insufficient"
                if not ready
                else "missing_market_data"
                if any(row.data_status == "missing" for row in selected)
                else "pending_data"
                if any(row.data_status == "pending" for row in selected)
                else "screen_positive"
                if train["criteria_met"] and holdout["criteria_met"]
                else "screen_negative"
            ),
            "train": train,
            "holdout": holdout,
        })
    return {
        "method": "time_blocked_post_decision_15m_open_to_fixed_4h_close_after_actual_notional_fees_v4",
        "screen_only": True,
        "minimum_entries_per_period": {"train": TRAIN_COUNT, "holdout": HOLDOUT_COUNT},
        "minimum_source_candles_per_period": {
            "train": TRAIN_MIN_SOURCE_CANDLES,
            "holdout": HOLDOUT_MIN_SOURCE_CANDLES,
        },
        "minimum_markets_per_period": MIN_MARKETS_PER_PERIOD,
        "raw_observations": len(logs),
        "duplicates_ignored": duplicates,
        "invalid_observations": incomplete,
        "cells": cells,
    }


def main() -> None:
    with SessionLocal() as db:
        print(json.dumps(shadow_report(db), sort_keys=True))


if __name__ == "__main__":
    main()
