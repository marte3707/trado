"""Forward outcome materialization for frozen research experiments.

This module is observational: it reads closed paper positions and candles, then
stores an immutable evaluation record. It must not affect signal generation,
position sizing, or execution eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import OrderMode
from app.db.models import Candle, ExperimentOutcome, Position

ZERO = Decimal("0")
TWO = Decimal("2")
BARRIER_METHOD = "first_touch_complete_candles_v2"
TIMEFRAME_DURATIONS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True)
class BarrierEvaluation:
    label: str
    first_touch_at: datetime | None
    mae_r: Decimal | None
    mfe_r: Decimal | None
    candles_observed: int


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _is_long(side: str) -> bool:
    return side.lower() in {"buy", "long"}


def _source_provider(metadata: dict[str, Any]) -> str:
    source = metadata.get("external_source")
    if not isinstance(source, dict):
        return "standard"
    provider = source.get("provider")
    if not isinstance(provider, str):
        return "standard"
    normalized = provider.strip().lower()
    return normalized or "standard"


def first_target_from_metadata(metadata: dict[str, Any]) -> Decimal | None:
    plan = metadata.get("take_profit_plan")
    if not isinstance(plan, list):
        return None
    for item in plan:
        if not isinstance(item, dict) or item.get("runner"):
            continue
        price = _decimal(item.get("price"))
        if price is not None and price > ZERO:
            return price
    return None


def evaluate_barriers(
    candles: list[Candle],
    *,
    side: str,
    entry_price: Decimal,
    initial_stop_loss: Decimal | None,
    first_target_price: Decimal | None,
) -> BarrierEvaluation:
    if not candles or initial_stop_loss is None or first_target_price is None:
        return BarrierEvaluation("unavailable", None, None, None, len(candles))
    risk_per_unit = abs(entry_price - initial_stop_loss)
    if risk_per_unit <= ZERO:
        return BarrierEvaluation("unavailable", None, None, None, len(candles))

    long = _is_long(side)
    label = "no_touch"
    first_touch_at: datetime | None = None
    max_adverse = ZERO
    max_favorable = ZERO
    for candle in candles:
        if long:
            adverse = max(ZERO, entry_price - candle.low)
            favorable = max(ZERO, candle.high - entry_price)
            stop_hit = candle.low <= initial_stop_loss
            target_hit = candle.high >= first_target_price
        else:
            adverse = max(ZERO, candle.high - entry_price)
            favorable = max(ZERO, entry_price - candle.low)
            stop_hit = candle.high >= initial_stop_loss
            target_hit = candle.low <= first_target_price
        max_adverse = max(max_adverse, adverse)
        max_favorable = max(max_favorable, favorable)
        if first_touch_at is not None:
            continue
        if stop_hit and target_hit:
            label = "ambiguous"
            first_touch_at = candle.open_time
        elif target_hit:
            label = "tp_first"
            first_touch_at = candle.open_time
        elif stop_hit:
            label = "stop_first"
            first_touch_at = candle.open_time

    return BarrierEvaluation(
        label=label,
        first_touch_at=first_touch_at,
        mae_r=max_adverse / risk_per_unit,
        mfe_r=max_favorable / risk_per_unit,
        candles_observed=len(candles),
    )


def build_experiment_outcome(
    position: Position,
    candles: list[Candle],
    *,
    taker_fee_rate: float,
) -> ExperimentOutcome | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    experiment = metadata.get("experiment")
    if not isinstance(experiment, dict) or position.closed_at is None:
        return None
    version = experiment.get("version")
    configuration_hash = experiment.get("configuration_hash")
    timeframe = metadata.get("source_timeframe")
    if not version or not configuration_hash or not timeframe:
        return None

    initial_stop = _decimal(metadata.get("initial_stop_loss"))
    first_target = first_target_from_metadata(metadata)
    duration = TIMEFRAME_DURATIONS.get(str(timeframe))
    # OHLC does not reveal when a level was touched inside a partial candle.
    # In particular, a candle opening at closed_at contains future prices.
    observed_candles = (
        [
            candle
            for candle in candles
            if candle.open_time >= position.opened_at
            and candle.open_time + duration <= position.closed_at
        ]
        if duration is not None
        else []
    )
    barrier = evaluate_barriers(
        observed_candles,
        side=position.side,
        entry_price=position.entry_price,
        initial_stop_loss=initial_stop,
        first_target_price=first_target,
    )
    initial_risk = _decimal(metadata.get("initial_position_risk"))
    gross_r = (
        position.realized_pnl / initial_risk
        if position.realized_pnl is not None and initial_risk is not None and initial_risk > ZERO
        else None
    )
    planned_notional = _decimal(metadata.get("planned_notional"))
    fee_notional = planned_notional * TWO if planned_notional is not None else None
    fee_method = "two_entry_notionals_legacy"
    if (
        metadata.get("paper_fill_source") == "fresh_l2_vwap"
        and planned_notional is not None
        and position.realized_pnl is not None
    ):
        exit_notional = planned_notional + (
            position.realized_pnl if _is_long(position.side) else -position.realized_pnl
        )
        if exit_notional >= ZERO:
            fee_notional = planned_notional + exit_notional
            fee_method = "entry_plus_realized_exit_notional_v1"
    estimated_fee_r = (
        fee_notional * Decimal(str(taker_fee_rate)) / initial_risk
        if fee_notional is not None and initial_risk is not None and initial_risk > ZERO
        else None
    )
    net_r = gross_r - estimated_fee_r if gross_r is not None and estimated_fee_r is not None else None
    signal_id = metadata.get("source_signal_id")
    try:
        parsed_signal_id = int(signal_id) if signal_id is not None else None
    except (TypeError, ValueError):
        parsed_signal_id = None

    return ExperimentOutcome(
        position_id=position.id,
        signal_id=parsed_signal_id,
        market_id=position.market_id,
        experiment_version=str(version),
        configuration_hash=str(configuration_hash),
        assignment=str(experiment.get("assignment") or "champion"),
        timeframe=str(timeframe),
        side=position.side,
        barrier_label=barrier.label,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        entry_price=position.entry_price,
        initial_stop_loss=initial_stop,
        first_target_price=first_target,
        gross_r=gross_r,
        estimated_fee_r=estimated_fee_r,
        net_r=net_r,
        mae_r=barrier.mae_r,
        mfe_r=barrier.mfe_r,
        first_touch_at=barrier.first_touch_at,
        metadata_json={
            "method": BARRIER_METHOD,
            "source_provider": _source_provider(metadata),
            "paper_fill_source": metadata.get("paper_fill_source"),
            "entry_candle_policy": "exclude_partial",
            "exit_candle_policy": "exclude_partial",
            "same_candle_policy": "ambiguous",
            "candles_observed": barrier.candles_observed,
            "estimated_round_trip_taker_fee_rate": str(taker_fee_rate),
            "estimated_fee_method": fee_method,
        },
    )


def materialize_experiment_outcomes(
    db: Session,
    *,
    taker_fee_rate: float,
    limit: int = 250,
) -> dict[str, int]:
    positions = (
        db.query(Position)
        .outerjoin(ExperimentOutcome, ExperimentOutcome.position_id == Position.id)
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "closed",
            Position.closed_at.is_not(None),
            Position.realized_pnl.is_not(None),
            Position.metadata_json.op("?")("experiment"),
            ExperimentOutcome.id.is_(None),
        )
        .order_by(Position.closed_at.asc(), Position.id.asc())
        .limit(limit)
        .all()
    )
    created = 0
    skipped = 0
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        timeframe = metadata.get("source_timeframe")
        if not isinstance(timeframe, str):
            skipped += 1
            continue
        candles = (
            db.query(Candle)
            .filter(
                Candle.market_id == position.market_id,
                Candle.timeframe == timeframe,
                Candle.open_time >= position.opened_at,
                Candle.open_time <= position.closed_at,
            )
            .order_by(Candle.open_time.asc())
            .all()
        )
        outcome = build_experiment_outcome(position, candles, taker_fee_rate=taker_fee_rate)
        if outcome is None:
            skipped += 1
            continue
        db.add(outcome)
        created += 1
    if created:
        # Il worker possiede il confine transazionale del ciclo. Un modulo
        # osservazionale non deve anticipare il commit di eventuale altro stato.
        db.flush()
    return {"candidates": len(positions), "created": created, "skipped": skipped}


def _summary(rows: list[ExperimentOutcome]) -> dict[str, Any]:
    usable = [row for row in rows if row.net_r is not None]
    net_values = [row.net_r for row in usable if row.net_r is not None]
    positive = sum((value for value in net_values if value > ZERO), start=ZERO)
    negative = abs(sum((value for value in net_values if value < ZERO), start=ZERO))
    cumulative = ZERO
    peak = ZERO
    max_drawdown = ZERO
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    labels = dict.fromkeys(
        ("tp_first", "stop_first", "ambiguous", "no_touch", "unavailable"), 0
    )
    for row in rows:
        labels[row.barrier_label] = labels.get(row.barrier_label, 0) + 1
    mae_values = [row.mae_r for row in rows if row.mae_r is not None]
    mfe_values = [row.mfe_r for row in rows if row.mfe_r is not None]
    return {
        "trades": len(rows),
        "usable_net_r": len(net_values),
        "net_r": float(sum(net_values, start=ZERO)),
        "average_net_r": float(sum(net_values, start=ZERO) / len(net_values)) if net_values else None,
        "profit_factor_net_r": float(positive / negative) if negative > ZERO else None,
        "max_drawdown_r": float(max_drawdown),
        "average_mae_r": float(sum(mae_values, start=ZERO) / len(mae_values)) if mae_values else None,
        "average_mfe_r": float(sum(mfe_values, start=ZERO) / len(mfe_values)) if mfe_values else None,
        "barriers": labels,
    }


def _active_paper_summary(positions: list[Position]) -> dict[str, Any]:
    """Describe open paper positions without treating their PnL as an outcome.

    A forward trade becomes evidence only once its closing outcome is
    materialized.  This separate payload makes the collection state visible
    while keeping unrealized PnL out of every edge calculation.
    """

    marked_to_market = [
        position.unrealized_pnl
        for position in positions
        if position.unrealized_pnl is not None
    ]
    configurations: dict[str, list[Position]] = {}
    sources: dict[str, list[Position]] = {}
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        experiment = metadata.get("experiment")
        configuration_hash = (
            str(experiment.get("configuration_hash"))
            if isinstance(experiment, dict) and experiment.get("configuration_hash")
            else "unknown"
        )
        configurations.setdefault(configuration_hash, []).append(position)
        sources.setdefault(_source_provider(metadata), []).append(position)

    def _group_summary(group: list[Position]) -> dict[str, Any]:
        group_pnl = [position.unrealized_pnl for position in group if position.unrealized_pnl is not None]
        return {
            "positions": len(group),
            "marked_to_market": len(group_pnl),
            "unrealized_pnl": float(sum(group_pnl, start=ZERO)) if group_pnl else None,
        }

    opened_at = [position.opened_at for position in positions]
    return {
        "positions": len(positions),
        "marked_to_market": len(marked_to_market),
        "unrealized_pnl": float(sum(marked_to_market, start=ZERO)) if marked_to_market else None,
        "oldest_opened_at": min(opened_at) if opened_at else None,
        "latest_opened_at": max(opened_at) if opened_at else None,
        "by_configuration": {
            key: _group_summary(value) for key, value in configurations.items()
        },
        "by_source": {key: _group_summary(value) for key, value in sources.items()},
    }


def experiment_outcomes_report(
    db: Session,
    *,
    version: str | None = None,
    recent: int = 50,
) -> dict[str, Any]:
    query = db.query(ExperimentOutcome)
    if version:
        query = query.filter(ExperimentOutcome.experiment_version == version)
    rows = query.order_by(ExperimentOutcome.closed_at.asc(), ExperimentOutcome.id.asc()).all()
    versions: dict[str, list[ExperimentOutcome]] = {}
    sources: dict[str, list[ExperimentOutcome]] = {}
    configurations: dict[str, list[ExperimentOutcome]] = {}
    for row in rows:
        versions.setdefault(row.experiment_version, []).append(row)
        configurations.setdefault(row.configuration_hash, []).append(row)
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        sources.setdefault(_source_provider(metadata), []).append(row)
    open_paper_positions = (
        db.query(Position)
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "open",
        )
        .order_by(Position.opened_at.asc(), Position.id.asc())
        .all()
    )
    active_positions: list[Position] = []
    for position in open_paper_positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        experiment = metadata.get("experiment")
        if not isinstance(experiment, dict):
            continue
        if version is not None and str(experiment.get("version")) != version:
            continue
        active_positions.append(position)
    return {
        "filters": {"version": version, "recent": recent},
        "overall": _summary(rows),
        "recent": _summary(rows[-recent:]),
        "by_version": {key: _summary(value) for key, value in versions.items()},
        "by_configuration": {key: _summary(value) for key, value in configurations.items()},
        "by_source": {key: _summary(value) for key, value in sources.items()},
        "active_paper": _active_paper_summary(active_positions),
        "configuration_count": len(configurations),
        "latest_closed_at": rows[-1].closed_at if rows else None,
    }
