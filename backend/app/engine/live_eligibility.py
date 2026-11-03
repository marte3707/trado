"""Paper-first live eligibility report.

It summarizes paper trades in R-multiple after simulated round-trip taker fees.
The report is read-only; ``evaluate_live_readiness_gate`` reuses the same metrics
as an explicit, opt-in live-entry guard.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.core.enums import OrderMode
from app.core.runtime_settings import setting_value
from app.db.models import ExperimentOutcome, Position, Signal
from app.engine.edge_profile import direction_family, score_bucket
from app.engine.macro_direction import btc_macro_direction, suspended_penalty_family

ELIGIBILITY_CLASSES = ("eligible", "watch", "quarantine", "insufficient")


def evaluate_live_readiness_gate(
    db: Session,
    *,
    taker_fee_rate: float,
) -> dict[str, Any]:
    live = setting_value(db, "live_execution")
    enabled = bool(live.get("readiness_gate_enabled", False))
    if not enabled:
        return {"enabled": False, "ready": True, "checks": [], "metrics": {}}

    def number(key: str, default: float) -> float:
        try:
            return float(live.get(key, default))
        except (TypeError, ValueError):
            return default

    lookback = max(20, int(number("readiness_lookback_trades", 200)))
    recent_window = max(10, int(number("readiness_recent_trades", 50)))
    min_closed = max(10, int(number("readiness_min_closed_trades", 100)))
    min_recent = max(5, int(number("readiness_min_recent_trades", 30)))
    min_pf = number("readiness_min_profit_factor", 1.2)
    min_recent_pf = number("readiness_min_recent_profit_factor", 1.1)
    min_avg_r = number("readiness_min_avg_net_r", 0.05)
    min_recent_avg_r = number("readiness_min_recent_avg_net_r", 0.02)
    max_drawdown_r = max(0.0, number("readiness_max_drawdown_r", 10.0))
    report = build_live_eligibility_report(
        db,
        lookback=lookback,
        recent_window=recent_window,
        taker_fee_rate=taker_fee_rate,
        min_trades=min_closed,
        min_recent_trades=min_recent,
        min_net_profit_factor=min_pf,
        min_recent_net_profit_factor=min_recent_pf,
        min_avg_net_r=min_avg_r,
        min_recent_avg_net_r=min_recent_avg_r,
    )
    full = report["overall"]["full"]
    recent = report["overall"]["recent"]
    train = report["overall"].get("train")
    test = report["overall"].get("test")
    checks: list[str] = []
    summary = report.get("summary", {})
    if isinstance(summary, dict) and summary.get("mixed_configurations"):
        checks.append("paper outcomes mix different configuration hashes")
    if isinstance(summary, dict) and summary.get("mixed_forward_gate_phases"):
        checks.append("paper outcomes mix different forward gate phases")
    if int(full["trades"]) < min_closed:
        checks.append(f"paper sample {full['trades']} < {min_closed}")
    if int(recent["trades"]) < min_recent:
        checks.append(f"recent paper sample {recent['trades']} < {min_recent}")
    if _pf_for_decision(full) < min_pf:
        checks.append(f"net PF {_pf_for_decision(full):.3f} < {min_pf:.3f}")
    if _pf_for_decision(recent) < min_recent_pf:
        checks.append(f"recent net PF {_pf_for_decision(recent):.3f} < {min_recent_pf:.3f}")
    if _avg_for_decision(full) < min_avg_r:
        checks.append(f"avg net R {_avg_for_decision(full):.3f} < {min_avg_r:.3f}")
    if _avg_for_decision(recent) < min_recent_avg_r:
        checks.append(
            f"recent avg net R {_avg_for_decision(recent):.3f} < {min_recent_avg_r:.3f}"
        )
    if isinstance(train, dict) and isinstance(test, dict):
        if _pf_for_decision(train) < min_pf or _avg_for_decision(train) < min_avg_r:
            checks.append("train holdout does not confirm net edge")
        if _pf_for_decision(test) < min_recent_pf or _avg_for_decision(test) < min_recent_avg_r:
            checks.append("recent holdout does not confirm net edge")
    if float(full["max_drawdown_r"]) > max_drawdown_r:
        checks.append(
            f"max drawdown {float(full['max_drawdown_r']):.3f}R > {max_drawdown_r:.3f}R"
        )
    return {
        "enabled": True,
        "ready": not checks,
        "checks": checks,
        "metrics": {"full": full, "recent": recent},
    }


@dataclass(frozen=True)
class EligibilityTrade:
    position_id: int | None
    direction: str
    timeframe: str
    score_bucket: str
    strategies: tuple[str, ...]
    gross_r: float
    fee_r: float
    net_r: float


def build_live_eligibility_report(
    db: Session,
    *,
    lookback: int = 2000,
    recent_window: int = 200,
    taker_fee_rate: float = 0.00045,
    min_trades: int = 30,
    min_recent_trades: int = 10,
    min_net_profit_factor: float = 1.05,
    min_recent_net_profit_factor: float = 1.0,
    min_avg_net_r: float = 0.02,
    min_recent_avg_net_r: float = 0.0,
    configuration_hash: str | None = None,
    experiment_version: str | None = None,
) -> dict[str, Any]:
    experiment = setting_value(db, "research_experiment")
    active_experiment_version = str(
        experiment.get("active_version") or "baseline-frozen-v1"
    )
    selected_experiment_version = experiment_version or active_experiment_version
    experiment_assignment = str(experiment.get("assignment") or "champion")
    query = (
        db.query(Position)
        .join(ExperimentOutcome, ExperimentOutcome.position_id == Position.id)
        .options(joinedload(Position.market))
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "closed",
            Position.realized_pnl.is_not(None),
            ExperimentOutcome.experiment_version == selected_experiment_version,
            ExperimentOutcome.assignment == experiment_assignment,
        )
    )
    if configuration_hash:
        query = query.filter(ExperimentOutcome.configuration_hash == configuration_hash)
    positions = (
        query
        .order_by(Position.closed_at.desc())
        .limit(lookback)
        .all()
    )
    signal_ids = {
        signal_id
        for position in positions
        if (signal_id := _source_signal_id(position)) is not None
    }
    signals_by_id: dict[int, Signal] = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )
    risk_settings = setting_value(db, "risk")
    macro = btc_macro_direction(db, risk_settings)
    macro_context = {
        **macro,
        "suspended_penalty_family": suspended_penalty_family(macro, risk_settings),
    }
    report = compute_live_eligibility_report(
        positions,
        signals_by_id,
        lookback=lookback,
        recent_window=recent_window,
        taker_fee_rate=taker_fee_rate,
        min_trades=min_trades,
        min_recent_trades=min_recent_trades,
        min_net_profit_factor=min_net_profit_factor,
        min_recent_net_profit_factor=min_recent_net_profit_factor,
        min_avg_net_r=min_avg_net_r,
        min_recent_avg_net_r=min_recent_avg_net_r,
        macro_context=macro_context,
        risk_settings=risk_settings,
    )
    report["filters"].update(
        {
            "sample_source": "experiment_outcomes",
            "experiment_version": selected_experiment_version,
            "active_experiment_version": active_experiment_version,
            "experiment_assignment": experiment_assignment,
            "configuration_hash": configuration_hash,
        }
    )
    configuration_query = db.query(ExperimentOutcome.configuration_hash).filter(
        ExperimentOutcome.experiment_version == selected_experiment_version,
        ExperimentOutcome.assignment == experiment_assignment,
        ExperimentOutcome.net_r.is_not(None),
    )
    if configuration_hash:
        configuration_query = configuration_query.filter(
            ExperimentOutcome.configuration_hash == configuration_hash
        )
    hashes = sorted({str(row[0]) for row in configuration_query.distinct().all() if row[0]})
    phases = _forward_gate_phase_keys(positions, signals_by_id)
    report["summary"].update(
        {
            "configuration_hashes": hashes,
            "mixed_configurations": len(hashes) > 1,
            "forward_gate_phases": sorted(phases),
            "mixed_forward_gate_phases": len(phases) > 1,
        }
    )
    if len(hashes) > 1:
        _mark_mixed_configuration_cohorts(report)
    if len(phases) > 1:
        _mark_mixed_configuration_cohorts(
            report,
            reason="risultati con gate forward in fasi diverse: separare le coorti prima di promuovere",
        )
    report["note"] = (
        "Il readiness gate usa esclusivamente trade paper attribuiti alla versione "
        "sperimentale attiva. Lo storico precedente resta disponibile per audit, "
        "ma non puo' promuovere nuove entry live."
    )
    return report


def _forward_gate_phase_keys(
    positions: list[Position],
    signals_by_id: dict[int, Signal],
) -> set[str]:
    """Identify changes to effective gate behavior within one forward version."""
    phases: set[str] = set()
    for position in positions:
        signal_id = _source_signal_id(position)
        signal = signals_by_id.get(signal_id) if signal_id is not None else None
        breakdown = signal.strategy_breakdown if signal is not None else None
        gate = breakdown.get("_forward_gate") if isinstance(breakdown, dict) else None
        if not isinstance(gate, dict):
            phases.add("unknown")
            continue
        expected = "shadow" if bool(gate.get("expected_r_shadow_only")) else "active"
        pre_tp1 = "shadow" if bool(gate.get("pre_tp1_shadow_only")) else "active"
        phases.add(f"expected_r:{expected}|pre_tp1:{pre_tp1}")
    return phases


def _mark_mixed_configuration_cohorts(
    report: dict[str, Any],
    *,
    reason: str = "risultati di configurazioni diverse: selezionare un hash singolo prima di promuovere",
) -> None:
    """Never make an eligibility claim from outcomes of different configs."""

    demoted = 0
    for cohort_group in ("direction_timeframes", "strategies", "score_buckets"):
        cohorts = report.get(cohort_group)
        if not isinstance(cohorts, list):
            continue
        for cohort in cohorts:
            if not isinstance(cohort, dict) or cohort.get("classification") != "eligible":
                continue
            cohort["classification"] = "watch"
            cohort["recommended_action"] = _recommended_action("watch")
            cohort["reasons"] = [reason]
            demoted += 1
    if demoted:
        summary = report.get("summary")
        if isinstance(summary, dict):
            summary["eligible"] = max(0, int(summary.get("eligible", 0)) - demoted)
            summary["watch"] = int(summary.get("watch", 0)) + demoted


def compute_live_eligibility_report(
    positions: list[Position],
    signals_by_id: dict[int, Signal],
    *,
    lookback: int = 2000,
    recent_window: int = 200,
    taker_fee_rate: float = 0.00045,
    min_trades: int = 30,
    min_recent_trades: int = 10,
    min_net_profit_factor: float = 1.05,
    min_recent_net_profit_factor: float = 1.0,
    min_avg_net_r: float = 0.02,
    min_recent_avg_net_r: float = 0.0,
    macro_context: dict[str, Any] | None = None,
    risk_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trades: list[EligibilityTrade] = []
    excluded: dict[str, int] = {
        "missing_signal": 0,
        "unsupported_direction": 0,
        "missing_risk": 0,
        "missing_notional": 0,
    }
    for position in positions:
        signal_id = _source_signal_id(position)
        signal = signals_by_id.get(signal_id) if signal_id is not None else None
        if signal is None:
            excluded["missing_signal"] += 1
            continue
        direction = direction_family(signal.direction)
        if direction is None:
            excluded["unsupported_direction"] += 1
            continue
        risk = _initial_risk_usd(position)
        if risk is None or risk <= 0:
            excluded["missing_risk"] += 1
            continue
        notional = _planned_notional_usd(position)
        if notional is None or notional <= 0:
            excluded["missing_notional"] += 1
            continue
        pnl = _decimal(position.realized_pnl)
        if pnl is None:
            excluded["missing_risk"] += 1
            continue
        gross_r = float(pnl / risk)
        fee_usd = notional * Decimal(str(taker_fee_rate)) * Decimal("2")
        fee_r = float(fee_usd / risk)
        trades.append(
            EligibilityTrade(
                position_id=getattr(position, "id", None),
                direction=direction,
                timeframe=str(signal.timeframe),
                score_bucket=score_bucket(signal.score),
                strategies=_aligned_strategies(signal),
                gross_r=gross_r,
                fee_r=fee_r,
                net_r=gross_r - fee_r,
            )
        )

    thresholds = {
        "min_trades": min_trades,
        "min_recent_trades": min_recent_trades,
        "min_net_profit_factor": min_net_profit_factor,
        "min_recent_net_profit_factor": min_recent_net_profit_factor,
        "min_avg_net_r": min_avg_net_r,
        "min_recent_avg_net_r": min_recent_avg_net_r,
    }
    base_kwargs: dict[str, Any] = {
        "recent_trades": trades[:recent_window],
        "thresholds": thresholds,
        "risk_settings": risk_settings or {},
    }
    direction_timeframes = [
        _cohort_payload(
            key=key,
            cohort_type="direction_timeframe",
            trades=value,
            direction=parts[0],
            timeframe=parts[1],
            **base_kwargs,
        )
        for key, value, parts in _sorted_group_items(
            _group_by(trades, lambda trade: f"{trade.direction}:{trade.timeframe}")
        )
    ]
    strategies = [
        _cohort_payload(
            key=key,
            cohort_type="strategy",
            trades=value,
            direction=parts[0],
            timeframe=parts[1],
            strategy=parts[2],
            **base_kwargs,
        )
        for key, value, parts in _sorted_group_items(_strategy_groups(trades))
    ]
    score_buckets = [
        _cohort_payload(
            key=key,
            cohort_type="score_bucket",
            trades=value,
            direction=parts[0],
            timeframe=parts[1],
            score_bucket_name=parts[2],
            **base_kwargs,
        )
        for key, value, parts in _sorted_group_items(
            _group_by(
                trades,
                lambda trade: f"{trade.direction}:{trade.timeframe}:{trade.score_bucket}",
            )
        )
    ]
    all_cohorts = direction_timeframes + strategies + score_buckets
    overall_split = _split_train_test(trades, min_recent_trades)
    setting_conflicts = _setting_conflicts(all_cohorts)
    summary = dict.fromkeys(ELIGIBILITY_CLASSES, 0)
    for cohort in all_cohorts:
        classification = str(cohort["classification"])
        summary[classification] = summary.get(classification, 0) + 1

    return {
        "filters": {
            "lookback": lookback,
            "recent_window": recent_window,
            "taker_fee_rate": taker_fee_rate,
        },
        "thresholds": thresholds,
        "macro_context": macro_context or {"direction": "unknown"},
        "summary": {
            **summary,
            "total_cohorts": len(all_cohorts),
            "paper_trades_loaded": len(positions),
            "paper_trades_usable": len(trades),
            "setting_conflicts": len(setting_conflicts),
            "excluded": excluded,
        },
        "overall": {
            "full": _stats_payload(trades),
            "recent": _stats_payload(trades[:recent_window]),
            "train": _stats_payload(overall_split[0]) if overall_split else None,
            "test": _stats_payload(overall_split[1]) if overall_split else None,
        },
        "direction_timeframes": direction_timeframes,
        "strategies": strategies,
        "score_buckets": score_buckets,
        "setting_conflicts": setting_conflicts,
        "note": (
            "Preview read-only: eligible/watch/quarantine sono calcolati sui paper "
            "trade in R netto stimato dopo fee taker round-trip. Il report non "
            "blocca e non abilita live execution."
        ),
    }


def _source_signal_id(position: Position) -> int | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    raw = metadata.get("source_signal_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _initial_size(position: Position) -> Decimal | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    size = _decimal(metadata.get("initial_size"))
    if size is not None and size > 0:
        return size
    size = _decimal(position.size)
    return size if size is not None and size > 0 else None


def _initial_risk_usd(position: Position) -> Decimal | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    risk = _decimal(metadata.get("initial_position_risk"))
    if risk is not None and risk > 0:
        return risk
    size = _initial_size(position)
    if size is None:
        return None
    entry = _decimal(position.entry_price)
    stop = _decimal(metadata.get("initial_stop_loss")) or _decimal(position.stop_loss)
    if entry is not None and stop is not None:
        risk = abs(entry - stop) * size
        if risk > 0:
            return risk
    atr = _decimal(metadata.get("entry_atr"))
    if atr is None or atr <= 0:
        return None
    risk = atr * size
    return risk if risk > 0 else None


def _planned_notional_usd(position: Position) -> Decimal | None:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    notional = _decimal(metadata.get("planned_notional"))
    if notional is not None and notional > 0:
        return notional
    size = _initial_size(position)
    entry = _decimal(position.entry_price)
    if size is None or entry is None:
        return None
    notional = entry * size
    return notional if notional > 0 else None


def _aligned_strategies(signal: Signal) -> tuple[str, ...]:
    breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
    names: list[str] = []
    signal_direction = direction_family(signal.direction)
    for name, payload in sorted(breakdown.items()):
        if name.startswith("_") or not isinstance(payload, dict):
            continue
        try:
            contribution = float(payload.get("contribution", 0))
        except (TypeError, ValueError):
            continue
        if contribution <= 0:
            continue
        if direction_family(payload.get("direction")) != signal_direction:
            continue
        names.append(name)
    return tuple(names)


def _group_by(
    trades: list[EligibilityTrade],
    key_fn: Callable[[EligibilityTrade], str],
) -> dict[str, list[EligibilityTrade]]:
    groups: dict[str, list[EligibilityTrade]] = {}
    for trade in trades:
        groups.setdefault(str(key_fn(trade)), []).append(trade)
    return groups


def _strategy_groups(trades: list[EligibilityTrade]) -> dict[str, list[EligibilityTrade]]:
    groups: dict[str, list[EligibilityTrade]] = {}
    for trade in trades:
        for strategy in trade.strategies:
            groups.setdefault(f"{trade.direction}:{trade.timeframe}:{strategy}", []).append(trade)
    return groups


def _sorted_group_items(
    groups: dict[str, list[EligibilityTrade]]
) -> list[tuple[str, list[EligibilityTrade], list[str]]]:
    return [
        (key, value, key.split(":"))
        for key, value in sorted(
            groups.items(),
            key=lambda item: (
                _stats_payload(item[1])["avg_net_r"] or 0.0,
                len(item[1]),
            ),
            reverse=True,
        )
    ]


def _stats_payload(trades: list[EligibilityTrade]) -> dict[str, Any]:
    values = [trade.net_r for trade in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    fee_r = sum(trade.fee_r for trade in trades)
    net_r = sum(values)
    trade_count = len(trades)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in reversed(values):
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": trade_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / trade_count) if trade_count else 0.0,
        "gross_r": sum(trade.gross_r for trade in trades),
        "fee_r": fee_r,
        "net_r": net_r,
        "gross_profit_r": gross_profit,
        "gross_loss_r": gross_loss,
        "net_profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "avg_net_r": (net_r / trade_count) if trade_count else None,
        "median_net_r": median(values) if values else None,
        "avg_fee_r": (fee_r / trade_count) if trade_count else None,
        "max_drawdown_r": max_drawdown,
    }


def _split_train_test(
    trades: list[EligibilityTrade],
    min_samples_per_side: int,
) -> tuple[list[EligibilityTrade], list[EligibilityTrade]] | None:
    if len(trades) < min_samples_per_side * 2:
        return None
    chronological = list(reversed(trades))
    split_at = max(min_samples_per_side, int(len(chronological) * 0.7))
    if len(chronological) - split_at < min_samples_per_side:
        split_at = len(chronological) - min_samples_per_side
    if split_at < min_samples_per_side:
        return None
    return chronological[:split_at], chronological[split_at:]


def _cohort_payload(
    *,
    key: str,
    cohort_type: str,
    trades: list[EligibilityTrade],
    recent_trades: list[EligibilityTrade],
    thresholds: dict[str, Any],
    risk_settings: dict[str, Any],
    direction: str,
    timeframe: str,
    strategy: str | None = None,
    score_bucket_name: str | None = None,
) -> dict[str, Any]:
    recent_ids = {id(trade) for trade in recent_trades}
    cohort_recent = [trade for trade in trades if id(trade) in recent_ids]
    split = _split_train_test(trades, int(thresholds["min_recent_trades"]))
    train = split[0] if split else []
    test = split[1] if split else []
    full_stats = _stats_payload(trades)
    recent_stats = _stats_payload(cohort_recent)
    train_stats = _stats_payload(train) if split else None
    test_stats = _stats_payload(test) if split else None
    classification, reasons = _classify(
        full_stats,
        recent_stats,
        train_stats,
        test_stats,
        thresholds=thresholds,
    )
    active_overlays = _active_risk_overlays(
        risk_settings,
        direction=direction,
        timeframe=timeframe,
        strategy=strategy,
        score_bucket_name=score_bucket_name,
    )
    return {
        "key": key,
        "type": cohort_type,
        "direction": direction,
        "timeframe": timeframe,
        "strategy": strategy,
        "score_bucket": score_bucket_name,
        "classification": classification,
        "recommended_action": _recommended_action(classification),
        "reasons": reasons,
        "active_risk_overlays": active_overlays,
        "full": full_stats,
        "recent": recent_stats,
        "train": train_stats,
        "test": test_stats,
    }


def _active_risk_overlays(
    risk_settings: dict[str, Any],
    *,
    direction: str,
    timeframe: str,
    strategy: str | None,
    score_bucket_name: str | None,
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    direction_key = f"{direction}:{timeframe}"
    score_key = f"{direction}:{score_bucket_name}" if score_bucket_name else None
    strategy_key = f"{timeframe}:{direction}:{strategy}" if strategy else None
    _append_overlay(
        overlays,
        risk_settings.get("directional_score_adjustments"),
        "directional_score_adjustment",
        direction_key,
    )
    _append_overlay(
        overlays,
        risk_settings.get("directional_min_score_adjustments"),
        "directional_min_score_adjustment",
        direction_key,
    )
    _append_overlay(
        overlays,
        risk_settings.get("directional_blocklist"),
        "directional_blocklist",
        direction_key,
    )
    _append_overlay(
        overlays,
        risk_settings.get("timeframe_score_adjustments"),
        "timeframe_score_adjustment",
        timeframe,
    )
    _append_overlay(
        overlays,
        risk_settings.get("timeframe_min_score_overrides"),
        "timeframe_min_score_override",
        timeframe,
    )
    if score_key:
        _append_overlay(
            overlays,
            risk_settings.get("score_bucket_adjustments"),
            "score_bucket_adjustment",
            score_key,
        )
    if strategy_key:
        _append_overlay(
            overlays,
            risk_settings.get("direction_strategy_weight_overrides"),
            "direction_strategy_weight_override",
            strategy_key,
        )
    return overlays


def _append_overlay(
    overlays: list[dict[str, Any]],
    raw_mapping: object,
    overlay_type: str,
    key: str,
) -> None:
    if not isinstance(raw_mapping, dict) or key not in raw_mapping:
        return
    payload = raw_mapping.get(key)
    overlays.append(
        {
            "type": overlay_type,
            "key": key,
            "payload": payload,
            "adjustment": _overlay_adjustment(payload),
        }
    )


def _overlay_adjustment(payload: object) -> int | None:
    if isinstance(payload, dict):
        payload = payload.get("adjustment")
    if not isinstance(payload, (Decimal, float, int, str)):
        return None
    try:
        return int(payload)
    except (TypeError, ValueError):
        return None


def _setting_conflicts(cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for cohort in cohorts:
        overlays = [
            overlay
            for overlay in cohort.get("active_risk_overlays", [])
            if overlay.get("type") != "direction_strategy_weight_override"
        ]
        if not overlays:
            continue
        classification = str(cohort.get("classification"))
        if classification == "eligible":
            conflicts.append(
                {
                    "severity": "warn",
                    "type": "eligible_cohort_penalized",
                    "cohort_key": cohort.get("key"),
                    "cohort_type": cohort.get("type"),
                    "classification": classification,
                    "active_risk_overlays": overlays,
                    "message": (
                        "Coorte eligible coperta da una penalità/blocklist più ampia; "
                        "valutare override mirato invece di rimuovere il guardrail globale."
                    ),
                }
            )
        elif classification == "watch" and _positive_recent_or_full(cohort):
            conflicts.append(
                {
                    "severity": "info",
                    "type": "watch_cohort_penalized",
                    "cohort_key": cohort.get("key"),
                    "cohort_type": cohort.get("type"),
                    "classification": classification,
                    "active_risk_overlays": overlays,
                    "message": "Coorte in watchlist ancora coperta da penalità attive.",
                }
            )
    return conflicts


def _positive_recent_or_full(cohort: dict[str, Any]) -> bool:
    raw_full = cohort.get("full")
    raw_recent = cohort.get("recent")
    full: dict[str, Any] = raw_full if isinstance(raw_full, dict) else {}
    recent: dict[str, Any] = raw_recent if isinstance(raw_recent, dict) else {}
    return _avg_for_decision(full) > 0 and _avg_for_decision(recent) >= 0


def _classify(
    full: dict[str, Any],
    recent: dict[str, Any],
    train: dict[str, Any] | None,
    test: dict[str, Any] | None,
    *,
    thresholds: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    min_trades = int(thresholds["min_trades"])
    min_recent_trades = int(thresholds["min_recent_trades"])
    if int(full["trades"]) < min_trades:
        return "insufficient", [f"sample full {full['trades']} < {min_trades}"]

    full_pf = _pf_for_decision(full)
    recent_pf = _pf_for_decision(recent)
    full_avg = _avg_for_decision(full)
    recent_avg = _avg_for_decision(recent)
    if int(recent["trades"]) < min_recent_trades:
        reasons.append(f"sample recente {recent['trades']} < {min_recent_trades}")
        if full_pf < 0.85 or full_avg <= -0.05:
            return "quarantine", reasons + ["edge full negativo"]
        return "watch", reasons

    if (
        full_pf >= float(thresholds["min_net_profit_factor"])
        and recent_pf >= float(thresholds["min_recent_net_profit_factor"])
        and full_avg >= float(thresholds["min_avg_net_r"])
        and recent_avg >= float(thresholds["min_recent_avg_net_r"])
    ):
        if (
            train is not None
            and test is not None
            and int(train["trades"]) >= min_recent_trades
            and int(test["trades"]) >= min_recent_trades
        ):
            train_pf = _pf_for_decision(train)
            train_avg = _avg_for_decision(train)
            test_pf = _pf_for_decision(test)
            test_avg = _avg_for_decision(test)
            if (
                train_pf < float(thresholds["min_net_profit_factor"])
                or train_avg < float(thresholds["min_avg_net_r"])
            ):
                return "watch", ["holdout train non conferma ancora l'edge"]
            if test_pf < float(thresholds["min_recent_net_profit_factor"]) or test_avg < 0:
                return "watch", ["holdout recente non conferma ancora l'edge"]
        return "eligible", ["edge netto positivo full + recente"]

    if full_pf < 0.85 or full_avg <= -0.05:
        reasons.append("edge full negativo dopo fee")
    if recent_pf < 0.8 or recent_avg <= -0.08:
        reasons.append("edge recente negativo dopo fee")
    if reasons:
        return "quarantine", reasons
    return "watch", ["edge marginale o non ancora confermato"]


def _pf_for_decision(stats: dict[str, Any]) -> float:
    raw = stats.get("net_profit_factor")
    return float("inf") if raw is None and int(stats.get("trades", 0)) > 0 else float(raw or 0.0)


def _avg_for_decision(stats: dict[str, Any]) -> float:
    raw = stats.get("avg_net_r")
    return float(raw) if raw is not None else 0.0


def _recommended_action(classification: str) -> str:
    if classification == "eligible":
        return "candidate_for_live_after_manual_review"
    if classification == "quarantine":
        return "keep_paper_only_until_recovery"
    if classification == "watch":
        return "observe_more_paper_trades"
    return "collect_more_samples"
