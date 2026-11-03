from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import AppSetting, ExperimentOutcome, Position, Signal
from app.engine.live_eligibility import (
    _forward_gate_phase_keys,
    _mark_mixed_configuration_cohorts,
    build_live_eligibility_report,
    compute_live_eligibility_report,
    evaluate_live_readiness_gate,
)


def _signal(
    signal_id: int,
    *,
    direction: str = "LONG",
    timeframe: str = "15m",
    score: int = 75,
    breakdown: dict | None = None,
) -> Signal:
    return Signal(
        id=signal_id,
        market_id=1,
        timeframe=timeframe,
        direction=direction,
        score=score,
        confidence_label="medium",
        entry_price=Decimal("100"),
        reason="test",
        strategy_breakdown=breakdown or {},
        status="new",
    )


def _position(
    *,
    realized_pnl: str,
    signal_id: int = 1,
    risk: str = "10",
    notional: str = "100",
) -> Position:
    return Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("90"),
        status="closed",
        realized_pnl=Decimal(realized_pnl),
        metadata_json={
            "source_signal_id": signal_id,
            "initial_position_risk": risk,
            "planned_notional": notional,
            "initial_size": "1",
            "initial_stop_loss": "90",
        },
    )


def test_live_eligibility_marks_cost_adjusted_positive_cohort_eligible() -> None:
    positions = [_position(realized_pnl="12") for _ in range(40)]
    report = compute_live_eligibility_report(
        positions,
        {1: _signal(1)},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
        taker_fee_rate=0.00045,
    )

    cohort = report["direction_timeframes"][0]

    assert cohort["key"] == "LONG:15m"
    assert cohort["classification"] == "eligible"
    assert cohort["full"]["avg_fee_r"] == pytest.approx(0.009)
    assert cohort["full"]["avg_net_r"] == pytest.approx(1.191)
    assert report["summary"]["eligible"] >= 1


def test_mixed_configuration_evidence_cannot_keep_a_cohort_eligible() -> None:
    report = compute_live_eligibility_report(
        [_position(realized_pnl="12") for _ in range(40)],
        {1: _signal(1)},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
        taker_fee_rate=0.00045,
    )

    _mark_mixed_configuration_cohorts(report)

    cohort = report["direction_timeframes"][0]
    assert cohort["classification"] == "watch"
    assert "configurazioni diverse" in cohort["reasons"][0]
    assert report["summary"]["eligible"] == 0


def test_db_report_flags_mixed_forward_gate_phases_even_with_one_hash() -> None:
    db = MagicMock()
    settings = {
        "research_experiment": AppSetting(
            key="research_experiment",
            value={"active_version": "paper-v1", "assignment": "candidate"},
        ),
        "risk": AppSetting(key="risk", value={}),
    }
    db.get.side_effect = lambda model, key: settings.get(key) if model is AppSetting else None
    positions = [
        *[_position(realized_pnl="12", signal_id=1) for _ in range(20)],
        *[_position(realized_pnl="12", signal_id=2) for _ in range(20)],
    ]
    signals = [
        _signal(
            1,
            breakdown={
                "_forward_gate": {
                    "expected_r_shadow_only": True,
                    "pre_tp1_shadow_only": True,
                }
            },
        ),
        _signal(
            2,
            breakdown={
                "_forward_gate": {
                    "expected_r_shadow_only": False,
                    "pre_tp1_shadow_only": False,
                }
            },
        ),
    ]
    position_query = MagicMock()
    position_query.join.return_value = position_query
    position_query.options.return_value = position_query
    position_query.filter.return_value = position_query
    position_query.order_by.return_value = position_query
    position_query.limit.return_value = position_query
    position_query.all.return_value = positions
    signal_query = MagicMock()
    signal_query.filter.return_value = signal_query
    signal_query.all.return_value = signals
    hash_query = MagicMock()
    hash_query.filter.return_value = hash_query
    hash_query.distinct.return_value = hash_query
    hash_query.all.return_value = [("same-hash",)]
    db.query.side_effect = [position_query, signal_query, hash_query]

    with patch("app.engine.live_eligibility.btc_macro_direction", return_value={"direction": "neutral"}):
        report = build_live_eligibility_report(db, min_trades=30, min_recent_trades=10)

    assert _forward_gate_phase_keys(positions, {signal.id: signal for signal in signals}) == {
        "expected_r:shadow|pre_tp1:shadow",
        "expected_r:active|pre_tp1:active",
    }
    assert report["summary"]["configuration_hashes"] == ["same-hash"]
    assert report["summary"]["mixed_configurations"] is False
    assert report["summary"]["mixed_forward_gate_phases"] is True
    assert report["direction_timeframes"][0]["classification"] == "watch"
    assert "gate forward in fasi diverse" in report["direction_timeframes"][0]["reasons"][0]


def test_live_readiness_rejects_mixed_forward_gate_phases() -> None:
    db = MagicMock()
    db.get.side_effect = lambda model, key: (
        AppSetting(key="live_execution", value={"readiness_gate_enabled": True})
        if model is AppSetting and key == "live_execution"
        else None
    )
    stats = {
        "trades": 100,
        "net_profit_factor": 2.0,
        "avg_net_r": 0.2,
        "max_drawdown_r": 1.0,
    }
    report = {
        "overall": {"full": stats, "recent": stats, "train": stats, "test": stats},
        "summary": {"mixed_configurations": True, "mixed_forward_gate_phases": True},
    }

    with patch("app.engine.live_eligibility.build_live_eligibility_report", return_value=report):
        readiness = evaluate_live_readiness_gate(db, taker_fee_rate=0.00045)

    assert readiness["ready"] is False
    assert "paper outcomes mix different configuration hashes" in readiness["checks"]
    assert "paper outcomes mix different forward gate phases" in readiness["checks"]


def test_live_eligibility_quarantines_negative_recent_edge() -> None:
    positions = [
        *[_position(realized_pnl="-1") for _ in range(12)],
        *[_position(realized_pnl="3") for _ in range(28)],
    ]
    report = compute_live_eligibility_report(
        positions,
        {1: _signal(1)},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
        taker_fee_rate=0.00045,
    )

    cohort = report["direction_timeframes"][0]

    assert cohort["classification"] == "quarantine"
    assert "edge recente negativo dopo fee" in cohort["reasons"]


def test_live_eligibility_does_not_promote_recent_recovery_without_train_confirmation() -> None:
    # Input is newest first. The recent 12 trades and aggregate look positive,
    # but the older train portion has no edge and must keep the cohort in paper.
    positions = [
        *[_position(realized_pnl="12") for _ in range(12)],
        *[_position(realized_pnl="-1") for _ in range(28)],
    ]
    report = compute_live_eligibility_report(
        positions,
        {1: _signal(1)},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
        taker_fee_rate=0.00045,
    )

    cohort = report["direction_timeframes"][0]

    assert cohort["full"]["avg_net_r"] > 0
    assert cohort["recent"]["avg_net_r"] > 0
    assert cohort["train"]["avg_net_r"] < 0
    assert cohort["classification"] == "watch"
    assert cohort["reasons"] == ["holdout train non conferma ancora l'edge"]


def test_live_eligibility_keeps_small_samples_insufficient() -> None:
    report = compute_live_eligibility_report(
        [_position(realized_pnl="12") for _ in range(8)],
        {1: _signal(1)},
        recent_window=8,
        min_trades=30,
        min_recent_trades=5,
    )

    cohort = report["direction_timeframes"][0]

    assert cohort["classification"] == "insufficient"
    assert cohort["recommended_action"] == "collect_more_samples"


def test_live_eligibility_strategy_cohorts_use_aligned_contributors_only() -> None:
    signal = _signal(
        1,
        direction="SHORT",
        breakdown={
            "vwap": {"direction": "SHORT", "contribution": 12},
            "rsi": {"direction": "LONG", "contribution": 10},
            "noise": {"direction": "SHORT", "contribution": 0},
            "_score": {"voting_weight": 20},
        },
    )
    report = compute_live_eligibility_report(
        [_position(realized_pnl="12") for _ in range(35)],
        {1: signal},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
    )

    keys = {cohort["key"] for cohort in report["strategies"]}

    assert "SHORT:15m:vwap" in keys
    assert "SHORT:15m:rsi" not in keys
    assert "SHORT:15m:noise" not in keys


def test_live_eligibility_flags_eligible_cohort_covered_by_broader_penalty() -> None:
    signal = _signal(
        1,
        direction="SHORT",
        breakdown={"wyckoff_spring_upthrust": {"direction": "SHORT", "contribution": 12}},
    )
    report = compute_live_eligibility_report(
        [_position(realized_pnl="12") for _ in range(35)],
        {1: signal},
        recent_window=12,
        min_trades=30,
        min_recent_trades=10,
        risk_settings={
            "directional_score_adjustments": {
                "SHORT:15m": {"adjustment": 12, "reason": "recent broad short weakness"}
            }
        },
    )

    cohort = next(c for c in report["strategies"] if c["key"] == "SHORT:15m:wyckoff_spring_upthrust")

    assert cohort["classification"] == "eligible"
    assert cohort["active_risk_overlays"][0]["type"] == "directional_score_adjustment"
    assert report["summary"]["setting_conflicts"] >= 1
    assert any(
        conflict["type"] == "eligible_cohort_penalized"
        and conflict["cohort_key"] == "SHORT:15m:wyckoff_spring_upthrust"
        for conflict in report["setting_conflicts"]
    )


def test_db_report_is_scoped_to_active_experiment_outcomes() -> None:
    db = MagicMock()
    settings = {
        "research_experiment": AppSetting(
            key="research_experiment",
            value={"active_version": "baseline-frozen-v2", "assignment": "champion"},
        ),
        "risk": AppSetting(key="risk", value={}),
    }
    db.get.side_effect = lambda model, key: settings.get(key) if model is AppSetting else None
    query = MagicMock()
    db.query.return_value = query
    query.join.return_value = query
    query.options.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.all.return_value = []

    with patch(
        "app.engine.live_eligibility.btc_macro_direction",
        return_value={"direction": "neutral"},
    ):
        report = build_live_eligibility_report(db, lookback=200)

    query.join.assert_called_once()
    join_args = query.join.call_args.args
    assert join_args[0] is ExperimentOutcome
    assert report["filters"]["sample_source"] == "experiment_outcomes"
    assert report["filters"]["experiment_version"] == "baseline-frozen-v2"
    assert report["overall"]["full"]["trades"] == 0


def test_db_report_can_inspect_a_prior_experiment_without_changing_the_active_one() -> None:
    db = MagicMock()
    settings = {
        "research_experiment": AppSetting(
            key="research_experiment",
            value={"active_version": "baseline-frozen-v3", "assignment": "champion"},
        ),
        "risk": AppSetting(key="risk", value={}),
    }
    db.get.side_effect = lambda model, key: settings.get(key) if model is AppSetting else None
    query = MagicMock()
    db.query.return_value = query
    query.join.return_value = query
    query.options.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.distinct.return_value = query
    query.all.return_value = []

    with patch(
        "app.engine.live_eligibility.btc_macro_direction",
        return_value={"direction": "neutral"},
    ):
        report = build_live_eligibility_report(
            db,
            lookback=200,
            experiment_version="baseline-frozen-v2",
        )

    assert report["filters"]["experiment_version"] == "baseline-frozen-v2"
    assert report["filters"]["active_experiment_version"] == "baseline-frozen-v3"
