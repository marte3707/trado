from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.enums import Direction
from app.db.models import ExperimentOutcome, Position, Signal
from app.engine.pre_tp1_validation import (
    build_experiment_pre_tp1_validation_table,
    build_pre_tp1_validation_table,
    evaluate_pre_tp1_entry,
)


def _signal(
    signal_id: int,
    *,
    direction: str = "SHORT",
    timeframe: str = "15m",
    score: int = 75,
    strategy: str = "volume_profile",
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
        strategy_breakdown={
            strategy: {
                "direction": direction,
                "contribution": 10,
            }
        },
        status="new",
    )


def _position(
    *,
    signal_id: int,
    realized_pnl: float,
    tp1_hit: bool = False,
    exit_reason: str = "stop_loss",
    market_id: int = 1,
    initial_risk: float = 100.0,
) -> Position:
    return Position(
        market_id=market_id,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("105"),
        status="closed",
        realized_pnl=Decimal(str(realized_pnl)),
        metadata_json={
            "source_signal_id": signal_id,
            "initial_position_risk": str(initial_risk),
            "tp1_hit": tp1_hit,
            "tp2_hit": False,
            "exit_reason": exit_reason,
        },
    )


def _weak_table():
    signal = _signal(1)
    positions = [
        *[
            _position(signal_id=1, realized_pnl=-100.0, tp1_hit=False)
            for _ in range(35)
        ],
        *[
            _position(
                signal_id=1,
                realized_pnl=100.0,
                tp1_hit=True,
                exit_reason="take_profit",
            )
            for _ in range(5)
        ],
    ]
    return build_pre_tp1_validation_table(
        positions,
        {1: signal},
        min_samples_for_use=30,
        lookback_positions=len(positions),
    )


def test_forward_pre_tp1_table_uses_first_touch_outcomes() -> None:
    signal = _signal(8, direction="LONG", timeframe="1h", score=68)
    labels = ["stop_first"] * 4 + ["tp_first"]
    outcomes = [
        ExperimentOutcome(
            position_id=index,
            signal_id=8,
            market_id=1,
            experiment_version="baseline-frozen-v2",
            configuration_hash="hash",
            assignment="champion",
            timeframe="1h",
            side="buy",
            barrier_label=label,
            opened_at=datetime(2026, 7, 20, tzinfo=UTC),
            closed_at=datetime(2026, 7, 21, tzinfo=UTC),
            entry_price=Decimal("100"),
            net_r=Decimal("1") if label == "tp_first" else Decimal("-1"),
            metadata_json={"method": "first_touch_complete_candles_v2"},
        )
        for index, label in enumerate(labels, start=1)
    ]

    table = build_experiment_pre_tp1_validation_table(
        outcomes,
        {8: signal},
        experiment_version="baseline-frozen-v2",
        assignment="champion",
        min_samples_for_use=5,
        lookback_positions=5,
    )

    metrics = table.cohorts["direction_timeframe_score"]["LONG:1h:60-69"]
    assert metrics.tp1_hits == 1
    assert metrics.stop_before_tp1 == 4
    assert metrics.avg_r == pytest.approx(-0.6)
    assert table.sample_source == "experiment_outcomes"


def test_forward_pre_tp1_excludes_legacy_barrier_method() -> None:
    signal = _signal(8, direction="LONG", timeframe="1h", score=68)
    legacy = ExperimentOutcome(
        position_id=1,
        signal_id=8,
        market_id=1,
        experiment_version="baseline-frozen-v2",
        configuration_hash="hash",
        assignment="champion",
        timeframe="1h",
        side="buy",
        barrier_label="tp_first",
        opened_at=datetime(2026, 7, 20, tzinfo=UTC),
        closed_at=datetime(2026, 7, 21, tzinfo=UTC),
        entry_price=Decimal("100"),
        net_r=Decimal("1"),
        metadata_json={"method": "first_touch_closed_candles_v1"},
    )

    table = build_experiment_pre_tp1_validation_table(
        [legacy],
        {8: signal},
        experiment_version="baseline-frozen-v2",
        assignment="champion",
        min_samples_for_use=1,
        lookback_positions=1,
    )

    assert table.excluded_count == 1
    assert table.cohorts["direction_timeframe_score"] == {}


def test_pre_tp1_validation_blocks_weak_direction_timeframe_score_bucket() -> None:
    table = _weak_table()

    passed, info = evaluate_pre_tp1_entry(
        table,
        direction=Direction.SHORT,
        timeframe="15m",
        score=75,
        market_id=1,
        strategy_breakdown=_signal(1).strategy_breakdown,
        risk_value={},
    )

    assert passed is False
    assert info["would_block"] is True
    assert info["failed_reasons"] == [
        {"cohort": "direction_timeframe_score", "key": "SHORT:15m:70-79"}
    ]
    assert info["primary"]["no_tp1_rate"] == pytest.approx(0.875)
    assert info["primary"]["stop_before_tp1_rate"] == pytest.approx(0.875)


def test_pre_tp1_validation_shadow_only_reports_without_blocking() -> None:
    table = _weak_table()

    passed, info = evaluate_pre_tp1_entry(
        table,
        direction="SHORT",
        timeframe="15m",
        score=75,
        market_id=1,
        strategy_breakdown=_signal(1).strategy_breakdown,
        risk_value={"pre_tp1_validation_shadow_only": True},
    )

    assert passed is True
    assert info["would_block"] is True
    assert info["shadow_only"] is True


def test_pre_tp1_validation_passes_when_sample_is_too_small() -> None:
    signal = _signal(1)
    table = build_pre_tp1_validation_table(
        [_position(signal_id=1, realized_pnl=-100.0) for _ in range(5)],
        {1: signal},
        min_samples_for_use=30,
        lookback_positions=5,
    )

    passed, info = evaluate_pre_tp1_entry(
        table,
        direction="SHORT",
        timeframe="15m",
        score=75,
        market_id=1,
        strategy_breakdown=signal.strategy_breakdown,
        risk_value={},
    )

    assert passed is True
    assert info["would_block"] is False
    assert info["primary"]["n_samples"] == 5


def test_pre_tp1_validation_uses_direction_timeframe_fallback_when_bucket_is_sparse() -> None:
    weak_bucket_signal = _signal(1, score=75)
    sparse_candidate_signal = _signal(2, score=85)
    positions = [
        *[
            _position(signal_id=1, realized_pnl=-100.0, tp1_hit=False)
            for _ in range(60)
        ],
        *[
            _position(signal_id=2, realized_pnl=-100.0, tp1_hit=False)
            for _ in range(5)
        ],
    ]
    table = build_pre_tp1_validation_table(
        positions,
        {1: weak_bucket_signal, 2: sparse_candidate_signal},
        min_samples_for_use=30,
    )

    passed, info = evaluate_pre_tp1_entry(
        table,
        direction="SHORT",
        timeframe="15m",
        score=85,
        market_id=1,
        strategy_breakdown=sparse_candidate_signal.strategy_breakdown,
        risk_value={},
    )

    assert passed is False
    assert info["primary"]["n_samples"] == 5
    assert info["failed_reasons"] == [
        {"cohort": "direction_timeframe", "key": "SHORT:15m"}
    ]


def test_pre_tp1_validation_passes_healthy_bucket() -> None:
    signal = _signal(1)
    positions = [
        *[
            _position(
                signal_id=1,
                realized_pnl=100.0,
                tp1_hit=True,
                exit_reason="take_profit",
            )
            for _ in range(25)
        ],
        *[
            _position(signal_id=1, realized_pnl=-100.0, tp1_hit=False)
            for _ in range(15)
        ],
    ]
    table = build_pre_tp1_validation_table(positions, {1: signal}, min_samples_for_use=30)

    passed, info = evaluate_pre_tp1_entry(
        table,
        direction="SHORT",
        timeframe="15m",
        score=75,
        market_id=1,
        strategy_breakdown=signal.strategy_breakdown,
        risk_value={},
    )

    assert passed is True
    assert info["primary"]["tp1_rate"] == pytest.approx(0.625)
