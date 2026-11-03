"""Forward-only experiment attribution and bootstrap gate isolation.

The module never mutates persisted trading settings. It tags generated signals,
measures readiness of the active epoch, and neutralizes legacy learned overlays
only in the effective per-evaluation risk copy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import AppSetting, ExperimentOutcome, StrategyConfig

DEFAULT_FORWARD_GATE_MIN_OUTCOMES = 30

HISTORICAL_OVERLAY_KEYS = (
    "directional_score_adjustments",
    "score_bucket_adjustments",
    "market_score_adjustments",
    "timeframe_score_adjustments",
    "directional_min_score_adjustments",
    "directional_blocklist",
    "direction_strategy_weight_overrides",
)

DEFAULT_RESEARCH_EXPERIMENT: dict[str, Any] = {
    "enabled": True,
    "active_version": "baseline-frozen-v1",
    "mode": "frozen_observation",
    "assignment": "champion",
    "hypothesis": "Misurare l'edge forward senza mutazioni automatiche dei parametri",
    "promotion_policy": "manual",
    "forward_gate_min_outcomes": DEFAULT_FORWARD_GATE_MIN_OUTCOMES,
    "expected_r_shadow_until_ready": True,
    "pre_tp1_shadow_until_ready": True,
    "historical_overlays_shadow_only": True,
    "performance_adjustments_shadow_only": True,
}

_POLICY_KEYS = (
    "forward_gate_min_outcomes",
    "forward_gates_shadow_only",
    "expected_r_shadow_until_ready",
    "pre_tp1_shadow_until_ready",
    "historical_overlays_shadow_only",
    "performance_adjustments_shadow_only",
    "paper_candidate",
    "required_paper_fill_source",
    "exit_slippage_stress_pct",
    "forward_train_holdout_embargo_hours",
)

def _experiment_settings(db: Session) -> dict[str, Any]:
    setting = db.get(AppSetting, "research_experiment")
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    return {**DEFAULT_RESEARCH_EXPERIMENT, **value}


def _safe_positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return default
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        return default
    return max(1, parsed)


def forward_gate_policy(
    db: Session,
    *,
    configuration_hash: str | None = None,
) -> dict[str, Any]:
    """Resolve the forward-only gate state for the active research epoch.

    Legacy evidence remains observable while the epoch is bootstrapping, but it
    cannot block generation. Once the minimum sample is reached, blocking gates
    must consume only outcomes attributed to the active version and assignment.
    """

    settings = _experiment_settings(db)
    enabled = bool(settings.get("enabled", True))
    mode = str(settings.get("mode", "frozen_observation"))
    version = str(settings.get("active_version", "baseline-frozen-v1"))
    assignment = str(settings.get("assignment", "champion"))
    active = enabled and mode == "frozen_observation"
    min_outcomes = _safe_positive_int(
        settings.get("forward_gate_min_outcomes"),
        DEFAULT_FORWARD_GATE_MIN_OUTCOMES,
    )
    outcome_count = 0
    if active:
        query = db.query(func.count(ExperimentOutcome.id)).filter(
            ExperimentOutcome.experiment_version == version,
            ExperimentOutcome.assignment == assignment,
            ExperimentOutcome.net_r.is_not(None),
        )
        if configuration_hash:
            query = query.filter(ExperimentOutcome.configuration_hash == configuration_hash)
        outcome_count = int(query.scalar() or 0)
    ready = active and outcome_count >= min_outcomes
    frozen_shadow_only = active and bool(settings.get("forward_gates_shadow_only", False))
    return {
        "active": active,
        "version": version,
        "assignment": assignment,
        "configuration_hash": configuration_hash,
        "outcome_count": outcome_count,
        "min_outcomes": min_outcomes,
        "forward_ready": ready,
        "forward_gates_shadow_only": frozen_shadow_only,
        "expected_r_shadow_only": (
            active
            and (
                frozen_shadow_only
                or (
                    not ready
                    and bool(settings.get("expected_r_shadow_until_ready", True))
                )
            )
        ),
        "pre_tp1_shadow_only": (
            active
            and (
                frozen_shadow_only
                or (
                    not ready
                    and bool(settings.get("pre_tp1_shadow_until_ready", True))
                )
            )
        ),
        "historical_overlays_shadow_only": (
            active and bool(settings.get("historical_overlays_shadow_only", True))
        ),
        "performance_adjustments_shadow_only": (
            active and bool(settings.get("performance_adjustments_shadow_only", True))
        ),
        "paper_candidate": (
            dict(settings["paper_candidate"])
            if active and isinstance(settings.get("paper_candidate"), dict)
            else None
        ),
        "sample_source": "experiment_outcomes" if ready else "legacy_closed_positions",
    }


def paper_candidate_allows_signal(
    policy: dict[str, Any],
    *,
    timeframe: str,
    direction: object,
    score: int | None = None,
    aligned_strategies: set[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Apply an explicitly configured candidate only to forward paper research.

    An enabled candidate with invalid or incomplete data fails closed. It must
    not lose its paper-only marker and accidentally become a live-eligible
    standard signal.
    """

    raw = policy.get("paper_candidate")
    if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
        return True, {"active": False}
    allowed_timeframes = raw.get("allowed_timeframes")
    if not isinstance(allowed_timeframes, list) or not allowed_timeframes:
        return False, {"active": True, "invalid": "allowed_timeframes", "reason": "invalid_candidate_configuration"}
    normalized_timeframes = {str(value) for value in allowed_timeframes if isinstance(value, str)}
    if timeframe not in normalized_timeframes:
        return False, {
            "active": True,
            "name": str(raw.get("name") or "paper_candidate"),
            "reason": "timeframe_not_in_candidate",
        }
    raw_directions = raw.get("allowed_directions_by_timeframe")
    if not isinstance(raw_directions, dict):
        return False, {"active": True, "invalid": "allowed_directions_by_timeframe", "reason": "invalid_candidate_configuration"}
    allowed_directions = raw_directions.get(timeframe)
    if not isinstance(allowed_directions, list) or not allowed_directions:
        return False, {"active": True, "invalid": "allowed_directions", "reason": "invalid_candidate_configuration"}
    value = str(getattr(direction, "value", direction) or "").upper()
    if value in {"LONG", "STRONG_LONG"}:
        family = "LONG"
    elif value in {"SHORT", "STRONG_SHORT"}:
        family = "SHORT"
    else:
        family = value
    normalized_directions = {str(item).upper() for item in allowed_directions if isinstance(item, str)}
    allowed = family in normalized_directions
    raw_min_scores = raw.get("min_score_by_timeframe")
    min_score: int | None = None
    if raw_min_scores is not None:
        if not isinstance(raw_min_scores, dict):
            return False, {"active": True, "invalid": "min_score_by_timeframe", "reason": "invalid_candidate_configuration"}
        raw_min_score = raw_min_scores.get(timeframe)
        if isinstance(raw_min_score, bool) or not isinstance(raw_min_score, int) or not 1 <= raw_min_score <= 100:
            return False, {"active": True, "invalid": "min_score_by_timeframe", "reason": "invalid_candidate_configuration"}
        min_score = raw_min_score
        if score is None or score < min_score:
            allowed = False
    raw_required_strategies = raw.get("required_aligned_strategies")
    required_strategies: set[str] = set()
    if raw_required_strategies is not None:
        if (
            not isinstance(raw_required_strategies, list)
            or not raw_required_strategies
            or any(not isinstance(item, str) or not item.strip() for item in raw_required_strategies)
        ):
            return False, {"active": True, "invalid": "required_aligned_strategies", "reason": "invalid_candidate_configuration"}
        required_strategies = {item.strip() for item in raw_required_strategies}
        if not required_strategies.issubset(aligned_strategies or set()):
            allowed = False
    return allowed, {
        "active": True,
        "name": str(raw.get("name") or "paper_candidate"),
        "timeframe": timeframe,
        "direction": family,
        "allowed_directions": sorted(normalized_directions),
        "min_score": min_score,
        "score": score,
        "required_aligned_strategies": sorted(required_strategies),
        "aligned_strategies": sorted(aligned_strategies or set()),
        "reason": (
            None
            if allowed
            else "direction_not_in_candidate"
            if family not in normalized_directions
            else "score_below_candidate_minimum"
            if min_score is not None and (score is None or score < min_score)
            else "required_strategy_not_aligned"
        ),
    }


def neutralize_historical_overlays(
    risk_value: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Disable pre-epoch learned overlays in an effective risk copy.

    The persisted settings are deliberately untouched so the evidence remains
    auditable and can be restored by ending the frozen experiment.
    """

    if not bool(policy.get("historical_overlays_shadow_only", False)):
        return {"shadow_only": False, "neutralized": []}
    neutralized = [
        key
        for key in HISTORICAL_OVERLAY_KEYS
        if isinstance(risk_value.get(key), dict) and bool(risk_value.get(key))
    ]
    for key in HISTORICAL_OVERLAY_KEYS:
        risk_value[key] = {}
    risk_value["edge_penalties_enabled"] = False
    return {
        "shadow_only": True,
        "neutralized": neutralized,
        "experiment_version": policy.get("version"),
        "reason": "pre_epoch_evidence_not_eligible_for_forward_gate",
    }


def _configuration_payload(
    strategies: Iterable[StrategyConfig],
    risk_value: dict[str, Any],
    position_management_value: dict[str, Any],
) -> dict[str, Any]:
    strategy_payload = [
        {
            "name": strategy.strategy_name,
            "timeframe": strategy.timeframe,
            "weight": strategy.weight,
            "params": strategy.params,
        }
        for strategy in strategies
    ]
    strategy_payload.sort(key=lambda item: (str(item["timeframe"]), str(item["name"])))
    return {
        # The whole effective runtime policy is part of the experiment. A curated
        # allow-list silently misses future gates, stop rules, or sizing inputs and
        # would contaminate a supposedly frozen forward sample.
        "risk": risk_value,
        "position_management": position_management_value,
        "strategies": strategy_payload,
    }


def experiment_snapshot(
    db: Session,
    *,
    strategies: Iterable[StrategyConfig],
    risk_value: dict[str, Any],
) -> dict[str, Any]:
    settings = _experiment_settings(db)
    position_management = db.get(AppSetting, "position_management")
    position_management_value = (
        dict(position_management.value)
        if position_management is not None and isinstance(position_management.value, dict)
        else {}
    )
    configuration = _configuration_payload(
        strategies,
        risk_value,
        position_management_value,
    )
    configuration["experiment_policy"] = {
        key: settings.get(key)
        for key in _POLICY_KEYS
        if key != "forward_gates_shadow_only" or key in settings
    }
    canonical = json.dumps(
        configuration,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "enabled": bool(settings.get("enabled", True)),
        "version": str(settings.get("active_version", "baseline-frozen-v1")),
        "mode": str(settings.get("mode", "frozen_observation")),
        "assignment": str(settings.get("assignment", "champion")),
        "hypothesis": str(settings.get("hypothesis", "")),
        "promotion_policy": str(settings.get("promotion_policy", "manual")),
        "configuration_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "captured_at": datetime.now(UTC).isoformat(),
    }


def signal_experiment_tag(signal_breakdown: object) -> dict[str, Any] | None:
    if not isinstance(signal_breakdown, dict):
        return None
    raw = signal_breakdown.get("_experiment")
    return dict(raw) if isinstance(raw, dict) else None
