"""Read-only verification of a pre-registered forward paper cohort.

The first N paper entries define the cohort before their outcomes are known.
Later winners cannot replace an early open or missing trade. This module never
changes research settings, order eligibility, positions, or live trading.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.db.models import AppSetting, ExperimentOutcome, Position, Signal
from app.db.session import SessionLocal

TRAIN_COUNT = 40
HOLDOUT_COUNT = 20
MIN_NET_PROFIT_FACTOR = Decimal("1.20")
MIN_AVG_NET_R = Decimal("0.02")
ZERO = Decimal("0")


def _source_signal_id(position: Position) -> int | None:
    raw = (position.metadata_json or {}).get("source_signal_id")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdecimal():
        return int(raw)
    return None


def _metrics_from_values(values: Sequence[Decimal]) -> dict[str, Any]:
    positive = sum((value for value in values if value > ZERO), ZERO)
    negative = abs(sum((value for value in values if value < ZERO), ZERO))
    total = sum(values, ZERO)
    average = total / len(values) if values else ZERO
    profit_factor = positive / negative if negative > ZERO else None
    pf_passed = (profit_factor is not None and profit_factor >= MIN_NET_PROFIT_FACTOR) or (
        negative == ZERO and positive > ZERO
    )
    cumulative = ZERO
    peak = ZERO
    max_drawdown = ZERO
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "trades": len(values),
        "net_r": str(total),
        "avg_net_r": str(average),
        "net_profit_factor": str(profit_factor) if profit_factor is not None else None,
        "no_losses": negative == ZERO,
        "max_drawdown_r": str(max_drawdown),
        "criteria_met": pf_passed and average >= MIN_AVG_NET_R,
    }


def _metrics(outcomes: Sequence[ExperimentOutcome]) -> dict[str, Any]:
    return _metrics_from_values(
        [outcome.net_r for outcome in outcomes if outcome.net_r is not None]
    )


def _exit_slippage_stress_r(position: Position, rate_pct: Decimal) -> Decimal | None:
    metadata = position.metadata_json or {}
    try:
        initial_risk = Decimal(str(metadata["initial_position_risk"]))
        entry_notional = Decimal(str(metadata["planned_notional"]))
        realized = Decimal(str(position.realized_pnl))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None
    if not all(value.is_finite() for value in (initial_risk, entry_notional, realized)):
        return None
    if initial_risk <= ZERO or entry_notional <= ZERO:
        return None
    if position.side.lower() in {"long", "buy"}:
        exit_notional = entry_notional + realized
    elif position.side.lower() in {"short", "sell"}:
        exit_notional = entry_notional - realized
    else:
        return None
    if exit_notional < ZERO:
        return None
    return exit_notional * rate_pct / Decimal("100") / initial_risk


def verify_forward_cohort(
    positions: Sequence[Position],
    outcomes_by_position: Mapping[int, ExperimentOutcome],
    signals_by_id: Mapping[int, Signal],
    *,
    train_count: int = TRAIN_COUNT,
    holdout_count: int = HOLDOUT_COUNT,
    require_shadow_only: bool = False,
    required_paper_fill_source: str | None = None,
    exit_slippage_stress_pct: Decimal | None = None,
    train_holdout_embargo_hours: int | None = None,
) -> dict[str, Any]:
    """Evaluate fixed entry-order train/holdout, never a best closed-trade subset."""
    required = train_count + holdout_count
    ordered = sorted(
        positions,
        key=lambda position: (position.opened_at or datetime.max.replace(tzinfo=UTC), position.id or 0),
    )
    embargo_until: datetime | None = None
    excluded_during_embargo = 0
    if train_holdout_embargo_hours is not None:
        if (
            isinstance(train_holdout_embargo_hours, bool)
            or not isinstance(train_holdout_embargo_hours, int)
            or train_holdout_embargo_hours <= 0
        ):
            return {"status": "invalid_train_holdout_embargo"}
        train_positions = ordered[:train_count]
        if len(train_positions) == train_count:
            last_train_open = train_positions[-1].opened_at
            if last_train_open is None:
                return {"status": "missing_train_open_timestamp"}
            embargo_until = last_train_open + timedelta(hours=train_holdout_embargo_hours)
            later = ordered[train_count:]
            holdout_pool = [
                position for position in later
                if position.opened_at is not None and position.opened_at > embargo_until
            ]
            excluded_during_embargo = len(later) - len(holdout_pool)
            first = train_positions + holdout_pool[:holdout_count]
        else:
            first = train_positions
    else:
        first = ordered[:required]
    base: dict[str, Any] = {
        "required_trades": required,
        "train_count": train_count,
        "holdout_count": holdout_count,
        "entries_observed": len(positions),
        "first_cohort_entries": len(first),
        "closed_entries": sum(position.status == "closed" for position in first),
        "outcomes_available": sum(
            position.id in outcomes_by_position for position in first if position.id is not None
        ),
        "train_holdout_embargo_hours": train_holdout_embargo_hours,
        "embargo_until": embargo_until.isoformat() if embargo_until else None,
        "entries_excluded_during_embargo": excluded_during_embargo,
        "thresholds": {
            "min_net_profit_factor": str(MIN_NET_PROFIT_FACTOR),
            "min_avg_net_r": str(MIN_AVG_NET_R),
        },
    }
    if exit_slippage_stress_pct is not None:
        if not exit_slippage_stress_pct.is_finite() or exit_slippage_stress_pct <= ZERO:
            return {**base, "status": "invalid_exit_slippage_stress"}
        base["thresholds"]["exit_slippage_stress_pct"] = str(exit_slippage_stress_pct)
    hashes: set[str] = set()
    phases: set[str] = set()
    versions: set[str] = set()
    assignments: set[str] = set()
    for position in first:
        if required_paper_fill_source and (position.metadata_json or {}).get(
            "paper_fill_source"
        ) != required_paper_fill_source:
            return {**base, "status": "invalid_paper_fill_source"}
        experiment = (position.metadata_json or {}).get("experiment")
        if not isinstance(experiment, dict) or not all(
            experiment.get(key) for key in ("configuration_hash", "version", "assignment")
        ):
            return {**base, "status": "invalid_configuration_attribution"}
        hashes.add(str(experiment["configuration_hash"]))
        versions.add(str(experiment["version"]))
        assignments.add(str(experiment["assignment"]))
        signal_id = _source_signal_id(position)
        signal = signals_by_id.get(signal_id) if signal_id is not None else None
        breakdown = signal.strategy_breakdown if signal is not None else None
        gate = breakdown.get("_forward_gate") if isinstance(breakdown, dict) else None
        if not isinstance(gate, dict) or not all(
            isinstance(gate.get(key), bool)
            for key in ("expected_r_shadow_only", "pre_tp1_shadow_only")
        ):
            return {**base, "status": "missing_forward_gate_attribution"}
        expected = "shadow" if gate["expected_r_shadow_only"] else "active"
        pre_tp1 = "shadow" if gate["pre_tp1_shadow_only"] else "active"
        phases.add(f"expected_r:{expected}|pre_tp1:{pre_tp1}")
    if len(hashes) > 1 or len(versions) > 1 or len(assignments) > 1:
        return {**base, "status": "mixed_configuration", "configuration_hashes": sorted(hashes)}
    if len(phases) > 1:
        return {**base, "status": "mixed_gate_phases", "gate_phases": sorted(phases)}
    if require_shadow_only and phases and phases != {"expected_r:shadow|pre_tp1:shadow"}:
        return {**base, "status": "non_shadow_gate_phase", "gate_phases": sorted(phases)}
    base["observed_gate_phases"] = sorted(phases)
    if len(first) < required:
        return {**base, "status": "insufficient_entries"}
    open_ids = [position.id for position in first if position.status != "closed"]
    if open_ids:
        return {**base, "status": "pending_closed_trades", "position_ids": open_ids}
    if train_holdout_embargo_hours is not None:
        first_holdout_open = first[train_count].opened_at
        if first_holdout_open is None or any(
            position.closed_at is None or position.closed_at >= first_holdout_open
            for position in first[:train_count]
        ):
            return {**base, "status": "overlapping_train_holdout_positions"}

    outcomes: list[ExperimentOutcome] = []
    missing_ids: list[int | None] = []
    for position in first:
        outcome = outcomes_by_position.get(position.id) if position.id is not None else None
        if outcome is None or outcome.net_r is None:
            missing_ids.append(position.id)
        else:
            outcomes.append(outcome)
    if missing_ids:
        return {**base, "status": "pending_outcomes", "position_ids": missing_ids}

    for position, outcome in zip(first, outcomes, strict=True):
        experiment = (position.metadata_json or {}).get("experiment")
        position_hash = experiment.get("configuration_hash") if isinstance(experiment, dict) else None
        if (
            not isinstance(experiment, dict)
            or not position_hash
            or position_hash != outcome.configuration_hash
            or experiment.get("version") != outcome.experiment_version
            or experiment.get("assignment") != outcome.assignment
            or position.id != outcome.position_id
            or _source_signal_id(position) != outcome.signal_id
        ):
            return {**base, "status": "invalid_configuration_attribution"}
        if (outcome.metadata_json or {}).get("source_provider") != "standard":
            return {**base, "status": "non_standard_source_in_cohort"}
        if required_paper_fill_source and (outcome.metadata_json or {}).get(
            "paper_fill_source"
        ) != required_paper_fill_source:
            return {**base, "status": "invalid_paper_fill_source"}

    train = _metrics(outcomes[:train_count])
    holdout = _metrics(outcomes[train_count:required])
    full = _metrics(outcomes)
    stress_result: dict[str, Any] | None = None
    stress_passed = True
    if exit_slippage_stress_pct is not None:
        stressed_values: list[Decimal] = []
        for position, outcome in zip(first, outcomes, strict=True):
            stress_r = _exit_slippage_stress_r(position, exit_slippage_stress_pct)
            if stress_r is None or outcome.net_r is None:
                return {**base, "status": "missing_exit_stress_attribution"}
            stressed_values.append(outcome.net_r - stress_r)
        stress_train = _metrics_from_values(stressed_values[:train_count])
        stress_holdout = _metrics_from_values(stressed_values[train_count:required])
        stress_full = _metrics_from_values(stressed_values)
        stress_passed = stress_train["criteria_met"] and stress_holdout["criteria_met"]
        stress_result = {
            "rate_pct": str(exit_slippage_stress_pct),
            "train": stress_train,
            "holdout": stress_holdout,
            "full": stress_full,
        }
    return {
        **base,
        "status": (
            "criteria_met"
            if train["criteria_met"] and holdout["criteria_met"] and stress_passed
            else "criteria_not_met"
        ),
        "experiment_version": next(iter(versions)),
        "assignment": next(iter(assignments)),
        "configuration_hash": next(iter(hashes)),
        "gate_phase": next(iter(phases)),
        "train": train,
        "holdout": holdout,
        "full": full,
        "exit_stress": stress_result,
    }


def current_forward_edge_report() -> dict[str, Any]:
    """Load only the active version's first fixed cohort; no writes or commits."""
    db = SessionLocal()
    try:
        setting = db.get(AppSetting, "research_experiment")
        config = setting.value if setting is not None and isinstance(setting.value, dict) else {}
        version = str(config.get("active_version") or "")
        assignment = str(config.get("assignment") or "")
        if not version or not assignment:
            return {"status": "missing_active_experiment"}
        stress_raw = config.get("exit_slippage_stress_pct")
        try:
            exit_stress = Decimal(str(stress_raw)) if stress_raw is not None else None
        except (InvalidOperation, ValueError, TypeError):
            return {"status": "invalid_exit_slippage_stress", "active_version": version}
        embargo_raw = config.get("forward_train_holdout_embargo_hours")
        if embargo_raw is not None and (
            isinstance(embargo_raw, bool)
            or not isinstance(embargo_raw, int)
            or embargo_raw <= 0
        ):
            return {"status": "invalid_train_holdout_embargo", "active_version": version}
        positions = db.execute(
            select(Position)
            .where(
                Position.mode == "paper",
                Position.metadata_json["experiment"]["version"].astext == version,
                Position.metadata_json["experiment"]["assignment"].astext == assignment,
            )
            .order_by(Position.opened_at, Position.id)
        ).scalars().all()
        position_ids = [position.id for position in positions if position.id is not None]
        outcomes = (
            db.execute(
                select(ExperimentOutcome).where(ExperimentOutcome.position_id.in_(position_ids))
            ).scalars().all()
            if position_ids
            else []
        )
        signal_ids = {
            signal_id
            for position in positions
            if (signal_id := _source_signal_id(position)) is not None
        }
        signals = (
            db.execute(
                select(Signal).where(Signal.id.in_(signal_ids))
            ).scalars().all()
            if signal_ids
            else []
        )
        result = verify_forward_cohort(
            positions,
            {outcome.position_id: outcome for outcome in outcomes},
            {signal.id: signal for signal in signals},
            require_shadow_only=bool(config.get("forward_gates_shadow_only", False)),
            required_paper_fill_source=(
                str(config["required_paper_fill_source"])
                if config.get("required_paper_fill_source")
                else None
            ),
            exit_slippage_stress_pct=exit_stress,
            train_holdout_embargo_hours=embargo_raw,
        )
        return {"active_version": version, "assignment": assignment, **result}
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps(current_forward_edge_report(), sort_keys=True))
