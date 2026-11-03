from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import Candle, Position
from app.engine.experiment_outcomes import (
    _active_paper_summary,
    build_experiment_outcome,
    evaluate_barriers,
)


def _candle(
    offset: int,
    *,
    high: str,
    low: str,
) -> Candle:
    opened = datetime(2026, 7, 20, tzinfo=UTC) + timedelta(hours=offset)
    return Candle(
        market_id=1,
        timeframe="1h",
        open_time=opened,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100"),
        volume=Decimal("10"),
    )


def test_long_barrier_evaluation_records_target_first_and_excursions() -> None:
    result = evaluate_barriers(
        [_candle(1, high="102", low="98"), _candle(2, high="111", low="99")],
        side="buy",
        entry_price=Decimal("100"),
        initial_stop_loss=Decimal("95"),
        first_target_price=Decimal("110"),
    )

    assert result.label == "tp_first"
    assert result.first_touch_at == datetime(2026, 7, 20, 2, tzinfo=UTC)
    assert result.mae_r == Decimal("0.4")
    assert result.mfe_r == Decimal("2.2")


def test_short_barrier_evaluation_records_stop_first() -> None:
    result = evaluate_barriers(
        [_candle(1, high="106", low="98"), _candle(2, high="101", low="89")],
        side="sell",
        entry_price=Decimal("100"),
        initial_stop_loss=Decimal("105"),
        first_target_price=Decimal("90"),
    )

    assert result.label == "stop_first"
    assert result.first_touch_at == datetime(2026, 7, 20, 1, tzinfo=UTC)
    assert result.mae_r == Decimal("1.2")
    assert result.mfe_r == Decimal("2.2")


def test_same_candle_touch_is_ambiguous() -> None:
    result = evaluate_barriers(
        [_candle(1, high="111", low="94")],
        side="buy",
        entry_price=Decimal("100"),
        initial_stop_loss=Decimal("95"),
        first_target_price=Decimal("110"),
    )

    assert result.label == "ambiguous"


def test_empty_barrier_window_is_unavailable() -> None:
    result = evaluate_barriers(
        [],
        side="buy",
        entry_price=Decimal("100"),
        initial_stop_loss=Decimal("95"),
        first_target_price=Decimal("110"),
    )

    assert result.label == "unavailable"
    assert result.mae_r is None
    assert result.mfe_r is None


def test_build_outcome_estimates_round_trip_fees_in_r() -> None:
    opened_at = datetime(2026, 7, 20, tzinfo=UTC)
    position = Position(
        id=12,
        market_id=1,
        mode="paper",
        side="buy",
        entry_price=Decimal("100"),
        size=Decimal("10"),
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=2),
        realized_pnl=Decimal("100"),
        metadata_json={
            "source_signal_id": 44,
            "source_timeframe": "1h",
            "initial_stop_loss": "95",
            "initial_position_risk": "50",
            "planned_notional": "1000",
            "paper_fill_source": "fresh_l2_vwap",
            "take_profit_plan": [
                {"level": "tp1", "price": "105", "allocation": "0.3", "filled": True},
                {"level": "runner", "price": "", "runner": True},
            ],
            "experiment": {
                "version": "baseline-frozen-v1",
                "configuration_hash": "abc123",
                "assignment": "champion",
            },
        },
    )

    outcome = build_experiment_outcome(
        position,
        [_candle(1, high="106", low="99")],
        taker_fee_rate=0.00045,
    )

    assert outcome is not None
    assert outcome.barrier_label == "tp_first"
    assert outcome.gross_r == Decimal("2")
    assert outcome.estimated_fee_r == Decimal("0.01890")
    assert outcome.net_r == Decimal("1.98110")
    assert outcome.position_id == 12
    assert outcome.signal_id == 44
    assert outcome.metadata_json["source_provider"] == "standard"
    assert outcome.metadata_json["paper_fill_source"] == "fresh_l2_vwap"
    assert outcome.metadata_json["estimated_fee_method"] == "entry_plus_realized_exit_notional_v1"
    assert outcome.metadata_json["method"] == "first_touch_complete_candles_v2"


def test_build_outcome_excludes_partial_entry_and_exit_candles() -> None:
    opened_at = datetime(2026, 7, 20, 0, 5, tzinfo=UTC)
    position = Position(
        id=14,
        market_id=1,
        mode="paper",
        side="buy",
        entry_price=Decimal("100"),
        size=Decimal("10"),
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at.replace(hour=2, minute=0),
        realized_pnl=Decimal("5"),
        metadata_json={
            "source_timeframe": "1h",
            "initial_stop_loss": "95",
            "initial_position_risk": "50",
            "planned_notional": "1000",
            "take_profit_plan": [{"level": "tp1", "price": "105", "allocation": "0.3"}],
            "experiment": {
                "version": "baseline-frozen-v1",
                "configuration_hash": "abc123",
                "assignment": "champion",
            },
        },
    )
    outcome = build_experiment_outcome(
        position,
        [
            _candle(0, high="110", low="94"),
            _candle(1, high="102", low="99"),
            _candle(2, high="110", low="94"),
        ],
        taker_fee_rate=0.00045,
    )

    assert outcome is not None
    assert outcome.barrier_label == "no_touch"
    assert outcome.mae_r == Decimal("0.2")
    assert outcome.mfe_r == Decimal("0.4")
    assert outcome.metadata_json["candles_observed"] == 1
    assert outcome.metadata_json["exit_candle_policy"] == "exclude_partial"


def test_build_outcome_tags_the_bull_without_persisting_message_content() -> None:
    opened_at = datetime(2026, 7, 20, tzinfo=UTC)
    position = Position(
        id=13,
        market_id=1,
        mode="paper",
        side="buy",
        entry_price=Decimal("100"),
        size=Decimal("10"),
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=2),
        realized_pnl=Decimal("100"),
        metadata_json={
            "source_signal_id": 45,
            "source_timeframe": "1h",
            "initial_stop_loss": "95",
            "initial_position_risk": "50",
            "planned_notional": "1000",
            "take_profit_plan": [{"level": "tp1", "price": "105", "allocation": "1"}],
            "external_source": {
                "provider": "the_bull",
                "message_id": 123,
                "raw_text": "Buying $TEST here",
            },
            "experiment": {
                "version": "baseline-frozen-v1",
                "configuration_hash": "abc123",
                "assignment": "champion",
            },
        },
    )

    outcome = build_experiment_outcome(
        position,
        [_candle(1, high="106", low="99")],
        taker_fee_rate=0.00045,
    )

    assert outcome is not None
    assert outcome.metadata_json["source_provider"] == "the_bull"
    assert "raw_text" not in outcome.metadata_json
    assert "message_id" not in outcome.metadata_json


def test_active_paper_summary_keeps_unrealized_pnl_separate_from_outcomes() -> None:
    opened_at = datetime(2026, 9, 21, 12, tzinfo=UTC)
    positions = [
        Position(
            id=1,
            market_id=1,
            mode="paper",
            side="buy",
            entry_price=Decimal("100"),
            size=Decimal("1"),
            status="open",
            opened_at=opened_at,
            unrealized_pnl=Decimal("1.25"),
            metadata_json={
                "experiment": {"configuration_hash": "candidate-a"},
            },
        ),
        Position(
            id=2,
            market_id=2,
            mode="paper",
            side="buy",
            entry_price=Decimal("50"),
            size=Decimal("2"),
            status="open",
            opened_at=opened_at + timedelta(minutes=5),
            unrealized_pnl=Decimal("-0.25"),
            metadata_json={
                "experiment": {"configuration_hash": "candidate-a"},
                "external_source": {"provider": "the_bull"},
            },
        ),
    ]

    summary = _active_paper_summary(positions)

    assert summary["positions"] == 2
    assert summary["marked_to_market"] == 2
    assert summary["unrealized_pnl"] == 1.0
    assert summary["by_configuration"]["candidate-a"]["positions"] == 2
    assert summary["by_source"]["standard"]["positions"] == 1
    assert summary["by_source"]["the_bull"]["positions"] == 1
