from decimal import Decimal

import pytest

from app.core.enums import Direction
from app.db.models import Position, Signal
from app.engine.pre_tp1_validation import (
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
