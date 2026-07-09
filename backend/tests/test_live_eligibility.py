from decimal import Decimal

import pytest

from app.db.models import Position, Signal
from app.engine.live_eligibility import compute_live_eligibility_report


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
