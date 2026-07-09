"""Empirical pre-TP1 entry validation.

This module answers a narrower question than generic expected-R calibration:
"for a candidate like this, did similar paper trades reach TP1 before stopping?"
The output is consumed by the signal engine as a conservative pre-entry gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import Direction
from app.db.models import Position, Signal
from app.engine.edge_profile import direction_family, linked_position_signals, score_bucket
from app.engine.performance import SignalPerformanceAnalyzer
from app.engine.score_calibration import _position_r_multiple

DEFAULT_LOOKBACK = 500
DEFAULT_CACHE_TTL_SECONDS = 30 * 60

DEFAULT_PRE_TP1_SETTINGS: dict[str, Any] = {
    "pre_tp1_validation_enabled": True,
    "pre_tp1_validation_shadow_only": False,
    "pre_tp1_validation_lookback": DEFAULT_LOOKBACK,
    "pre_tp1_validation_min_samples": 30,
    "pre_tp1_validation_strategy_min_samples": 25,
    "pre_tp1_validation_market_min_samples": 8,
    "pre_tp1_validation_max_no_tp1_rate": 0.55,
    "pre_tp1_validation_max_stop_before_tp1_rate": 0.50,
    "pre_tp1_validation_min_profit_factor": 0.80,
    "pre_tp1_validation_min_avg_r": 0.0,
    "pre_tp1_validation_strategy_block_enabled": False,
    "pre_tp1_validation_market_block_enabled": False,
}


@dataclass(frozen=True)
class PreTp1CohortMetrics:
    key: str
    cohort_type: str
    n_samples: int
    tp1_hits: int
    no_tp1: int
    stop_before_tp1: int
    wins: int
    realized_pnl: float
    gross_profit: float
    gross_loss: float
    avg_pnl: float
    avg_r: float | None
    median_r: float | None
    tp1_rate: float
    no_tp1_rate: float
    stop_before_tp1_rate: float
    win_rate: float
    profit_factor: float | None
    confidence: str


@dataclass(frozen=True)
class PreTp1ValidationTable:
    cohorts: dict[str, dict[str, PreTp1CohortMetrics]]
    computed_at: datetime
    lookback_positions: int
    excluded_count: int

    def get(self, cohort_type: str, key: str | None) -> PreTp1CohortMetrics | None:
        if not key:
            return None
        return self.cohorts.get(cohort_type, {}).get(key)


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _settings_value(risk_value: dict[str, Any], key: str) -> Any:
    return risk_value.get(key, DEFAULT_PRE_TP1_SETTINGS[key])


def _metadata_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, int | float):
        return bool(value)
    return False


def _tp1_hit(position: Position) -> bool:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    return _metadata_flag(metadata.get("tp1_hit")) or _metadata_flag(metadata.get("tp2_hit"))


def _exit_reason(position: Position) -> str:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    reason = metadata.get("exit_reason_detail") or metadata.get("exit_reason")
    return str(reason or "unknown")


def _is_stop_before_tp1(position: Position) -> bool:
    return not _tp1_hit(position) and _exit_reason(position) == "stop_loss"


def _aligned_strategy_names(signal: Signal) -> set[str]:
    breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
    family = direction_family(signal.direction)
    if family is None:
        return set()
    names: set[str] = set()
    for name, payload in breakdown.items():
        if str(name).startswith("_") or not isinstance(payload, dict):
            continue
        if direction_family(payload.get("direction")) != family:
            continue
        try:
            contribution = float(payload.get("contribution", 0.0))
        except (TypeError, ValueError):
            contribution = 0.0
        if contribution > 0:
            names.add(str(name))
    return names


def aligned_strategy_names_from_breakdown(
    breakdown: dict[str, Any], direction: Direction | str
) -> set[str]:
    family = direction_family(direction)
    if family is None:
        return set()
    names: set[str] = set()
    for name, payload in breakdown.items():
        if str(name).startswith("_") or not isinstance(payload, dict):
            continue
        if direction_family(payload.get("direction")) != family:
            continue
        try:
            contribution = float(payload.get("contribution", 0.0))
        except (TypeError, ValueError):
            contribution = 0.0
        if contribution > 0:
            names.add(str(name))
    return names


def _cohort_metrics(
    cohort_type: str,
    key: str,
    positions: list[Position],
    *,
    min_samples_for_use: int,
) -> PreTp1CohortMetrics:
    pnls = [float(position.realized_pnl or 0.0) for position in positions]
    r_values = [
        r for position in positions if (r := _position_r_multiple(position)) is not None
    ]
    n_samples = len(positions)
    wins = sum(1 for value in pnls if value > 0)
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value <= 0))
    tp1_hits = sum(1 for position in positions if _tp1_hit(position))
    no_tp1 = n_samples - tp1_hits
    stop_before_tp1 = sum(1 for position in positions if _is_stop_before_tp1(position))
    profit_factor = gross_profit / gross_loss if gross_loss else None
    if n_samples < min_samples_for_use:
        confidence = "low"
    elif n_samples < min_samples_for_use * 3:
        confidence = "medium"
    else:
        confidence = "high"
    return PreTp1CohortMetrics(
        key=key,
        cohort_type=cohort_type,
        n_samples=n_samples,
        tp1_hits=tp1_hits,
        no_tp1=no_tp1,
        stop_before_tp1=stop_before_tp1,
        wins=wins,
        realized_pnl=sum(pnls),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        avg_pnl=(sum(pnls) / n_samples) if n_samples else 0.0,
        avg_r=(sum(r_values) / len(r_values)) if r_values else None,
        median_r=median(r_values) if r_values else None,
        tp1_rate=(tp1_hits / n_samples) if n_samples else 0.0,
        no_tp1_rate=(no_tp1 / n_samples) if n_samples else 0.0,
        stop_before_tp1_rate=(stop_before_tp1 / n_samples) if n_samples else 0.0,
        win_rate=(wins / n_samples) if n_samples else 0.0,
        profit_factor=profit_factor,
        confidence=confidence,
    )


def build_pre_tp1_validation_table(
    positions: list[Position],
    signals_by_id: dict[int, Signal],
    *,
    min_samples_for_use: int = 30,
    lookback_positions: int = DEFAULT_LOOKBACK,
) -> PreTp1ValidationTable:
    grouped: dict[str, dict[str, list[Position]]] = {
        "direction_timeframe_score": {},
        "direction_timeframe": {},
        "market_direction_timeframe": {},
        "strategy": {},
    }
    excluded = 0
    for row in linked_position_signals_from_map(positions, signals_by_id):
        position = row[0]
        signal = row[1]
        if signal is None:
            excluded += 1
            continue
        family = direction_family(signal.direction)
        if family is None:
            excluded += 1
            continue
        score_key = f"{family}:{signal.timeframe}:{score_bucket(signal.score)}"
        tf_key = f"{family}:{signal.timeframe}"
        market_key = f"{position.market_id}:{family}:{signal.timeframe}"
        grouped["direction_timeframe_score"].setdefault(score_key, []).append(position)
        grouped["direction_timeframe"].setdefault(tf_key, []).append(position)
        grouped["market_direction_timeframe"].setdefault(market_key, []).append(position)
        for strategy_name in _aligned_strategy_names(signal):
            strategy_key = f"{signal.timeframe}:{family}:{strategy_name}"
            grouped["strategy"].setdefault(strategy_key, []).append(position)

    cohorts = {
        cohort_type: {
            key: _cohort_metrics(
                cohort_type,
                key,
                group_positions,
                min_samples_for_use=min_samples_for_use,
            )
            for key, group_positions in groups.items()
        }
        for cohort_type, groups in grouped.items()
    }
    return PreTp1ValidationTable(
        cohorts=cohorts,
        computed_at=datetime.now(UTC),
        lookback_positions=lookback_positions,
        excluded_count=excluded,
    )


def linked_position_signals_from_map(
    positions: list[Position], signals_by_id: dict[int, Signal]
) -> list[tuple[Position, Signal | None]]:
    rows: list[tuple[Position, Signal | None]] = []
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        raw_signal_id = metadata.get("source_signal_id")
        try:
            signal_id = int(raw_signal_id) if isinstance(raw_signal_id, (int, str)) else None
        except (TypeError, ValueError):
            signal_id = None
        rows.append((position, signals_by_id.get(signal_id) if signal_id is not None else None))
    return rows


def build_pre_tp1_validation_table_from_db(
    db: Session,
    *,
    lookback: int = DEFAULT_LOOKBACK,
    min_samples_for_use: int = 30,
    analyzer: SignalPerformanceAnalyzer | None = None,
) -> PreTp1ValidationTable:
    analyzer = analyzer or SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=lookback)
    rows = linked_position_signals(db, positions)
    signals_by_id: dict[int, Signal] = {}
    filtered_positions: list[Position] = []
    for row in rows:
        filtered_positions.append(row.position)
        if row.signal is not None:
            signals_by_id[row.signal.id] = row.signal
    return build_pre_tp1_validation_table(
        filtered_positions,
        signals_by_id,
        min_samples_for_use=min_samples_for_use,
        lookback_positions=lookback,
    )


def _metric_failed(
    metrics: PreTp1CohortMetrics | None,
    *,
    min_samples: int,
    max_no_tp1_rate: float,
    max_stop_before_tp1_rate: float,
    min_profit_factor: float,
    min_avg_r: float,
) -> bool:
    if metrics is None or metrics.n_samples < min_samples:
        return False
    pf = metrics.profit_factor if metrics.profit_factor is not None else 0.0
    avg_r = metrics.avg_r if metrics.avg_r is not None else metrics.avg_pnl
    return (
        metrics.no_tp1_rate >= max_no_tp1_rate
        and metrics.stop_before_tp1_rate >= max_stop_before_tp1_rate
        and pf < min_profit_factor
        and avg_r < min_avg_r
    )


def evaluate_pre_tp1_entry(
    table: PreTp1ValidationTable,
    *,
    direction: Direction | str,
    timeframe: str,
    score: int,
    market_id: int,
    strategy_breakdown: dict[str, Any],
    risk_value: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    family = direction_family(direction)
    if family is None:
        return True, {"enabled": False, "reason": "neutral_or_unknown_direction"}

    min_samples = max(1, _safe_int(_settings_value(risk_value, "pre_tp1_validation_min_samples"), 30))
    strategy_min_samples = max(
        1,
        _safe_int(_settings_value(risk_value, "pre_tp1_validation_strategy_min_samples"), 25),
    )
    market_min_samples = max(
        1,
        _safe_int(_settings_value(risk_value, "pre_tp1_validation_market_min_samples"), 8),
    )
    max_no_tp1_rate = _safe_float(
        _settings_value(risk_value, "pre_tp1_validation_max_no_tp1_rate"), 0.55
    )
    max_stop_before_tp1_rate = _safe_float(
        _settings_value(risk_value, "pre_tp1_validation_max_stop_before_tp1_rate"), 0.50
    )
    min_profit_factor = _safe_float(
        _settings_value(risk_value, "pre_tp1_validation_min_profit_factor"), 0.80
    )
    min_avg_r = _safe_float(_settings_value(risk_value, "pre_tp1_validation_min_avg_r"), 0.0)

    score_key = f"{family}:{timeframe}:{score_bucket(score)}"
    tf_key = f"{family}:{timeframe}"
    market_key = f"{market_id}:{family}:{timeframe}"
    primary = table.get("direction_timeframe_score", score_key)
    fallback = table.get("direction_timeframe", tf_key)
    market = table.get("market_direction_timeframe", market_key)
    strategy_names = aligned_strategy_names_from_breakdown(strategy_breakdown, direction)
    strategy_metrics: list[PreTp1CohortMetrics] = []
    for name in sorted(strategy_names):
        metric = table.get("strategy", f"{timeframe}:{family}:{name}")
        if metric is not None:
            strategy_metrics.append(metric)

    failed_reasons: list[dict[str, Any]] = []
    common_thresholds = {
        "max_no_tp1_rate": max_no_tp1_rate,
        "max_stop_before_tp1_rate": max_stop_before_tp1_rate,
        "min_profit_factor": min_profit_factor,
        "min_avg_r": min_avg_r,
    }
    primary_has_enough_samples = primary is not None and primary.n_samples >= min_samples
    if _metric_failed(primary, min_samples=min_samples, **common_thresholds):
        failed_reasons.append({"cohort": "direction_timeframe_score", "key": score_key})
    elif not primary_has_enough_samples and _metric_failed(
        fallback,
        min_samples=min_samples * 2,
        **common_thresholds,
    ):
        failed_reasons.append({"cohort": "direction_timeframe", "key": tf_key})

    if bool(risk_value.get("pre_tp1_validation_market_block_enabled", False)) and _metric_failed(
        market,
        min_samples=market_min_samples,
        **common_thresholds,
    ):
        failed_reasons.append({"cohort": "market_direction_timeframe", "key": market_key})

    bad_strategies = [
        metric
        for metric in strategy_metrics
        if _metric_failed(metric, min_samples=strategy_min_samples, **common_thresholds)
    ]
    if bool(risk_value.get("pre_tp1_validation_strategy_block_enabled", False)) and bad_strategies:
        failed_reasons.append(
            {
                "cohort": "strategy",
                "keys": [metric.key for metric in bad_strategies[:5]],
            }
        )

    would_block = bool(failed_reasons)
    shadow_only = bool(_settings_value(risk_value, "pre_tp1_validation_shadow_only"))
    return (not would_block or shadow_only), {
        "enabled": True,
        "passed": not would_block,
        "shadow_only": shadow_only,
        "would_block": would_block,
        "reason": "ok" if not would_block else "weak_pre_tp1_cohort",
        "failed_reasons": failed_reasons,
        "keys": {
            "direction_timeframe_score": score_key,
            "direction_timeframe": tf_key,
            "market_direction_timeframe": market_key,
        },
        "thresholds": {
            "min_samples": min_samples,
            "strategy_min_samples": strategy_min_samples,
            "market_min_samples": market_min_samples,
            **common_thresholds,
        },
        "primary": serialize_metrics(primary),
        "fallback": serialize_metrics(fallback),
        "market": serialize_metrics(market),
        "strategies": [serialize_metrics(metric) for metric in strategy_metrics[:8]],
        "computed_at": table.computed_at.isoformat(),
    }


def serialize_metrics(metrics: PreTp1CohortMetrics | None) -> dict[str, Any] | None:
    if metrics is None:
        return None

    def clean(value: float | None) -> float | None:
        if value is None or math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)

    return {
        "key": metrics.key,
        "cohort_type": metrics.cohort_type,
        "n_samples": metrics.n_samples,
        "tp1_hits": metrics.tp1_hits,
        "no_tp1": metrics.no_tp1,
        "stop_before_tp1": metrics.stop_before_tp1,
        "tp1_rate": clean(metrics.tp1_rate),
        "no_tp1_rate": clean(metrics.no_tp1_rate),
        "stop_before_tp1_rate": clean(metrics.stop_before_tp1_rate),
        "win_rate": clean(metrics.win_rate),
        "profit_factor": clean(metrics.profit_factor),
        "avg_r": clean(metrics.avg_r),
        "median_r": clean(metrics.median_r),
        "realized_pnl": clean(metrics.realized_pnl),
        "avg_pnl": clean(metrics.avg_pnl),
        "confidence": metrics.confidence,
    }


def serialize_table(table: PreTp1ValidationTable) -> dict[str, Any]:
    return {
        "computed_at": table.computed_at.isoformat(),
        "lookback_positions": table.lookback_positions,
        "excluded_count": table.excluded_count,
        "cohorts": {
            cohort_type: {
                key: serialize_metrics(metrics)
                for key, metrics in sorted(cohorts.items())
            }
            for cohort_type, cohorts in sorted(table.cohorts.items())
        },
    }


_CACHE: dict[str, Any] = {"table": None, "computed_at": None, "min_samples_for_use": None}


def cached_pre_tp1_validation_table(
    db: Session,
    *,
    force_refresh: bool = False,
    lookback: int = DEFAULT_LOOKBACK,
    min_samples_for_use: int = 30,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> PreTp1ValidationTable:
    now = datetime.now(UTC)
    table: PreTp1ValidationTable | None = _CACHE.get("table")
    computed_at: datetime | None = _CACHE.get("computed_at")
    is_stale = (
        force_refresh
        or table is None
        or computed_at is None
        or (now - computed_at).total_seconds() > cache_ttl_seconds
        or table.lookback_positions != lookback
        or _CACHE.get("min_samples_for_use") != min_samples_for_use
    )
    if not is_stale:
        assert table is not None
        return table
    table = build_pre_tp1_validation_table_from_db(
        db,
        lookback=lookback,
        min_samples_for_use=min_samples_for_use,
    )
    _CACHE["table"] = table
    _CACHE["computed_at"] = now
    _CACHE["min_samples_for_use"] = min_samples_for_use
    return table


def invalidate_pre_tp1_validation_cache() -> None:
    _CACHE["table"] = None
    _CACHE["computed_at"] = None
    _CACHE["min_samples_for_use"] = None
