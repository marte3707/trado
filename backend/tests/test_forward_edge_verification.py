from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import ExperimentOutcome, Position, Signal
from app.engine.forward_edge_verification import verify_forward_cohort


def _cohort(
    train_r: str = "0.2",
    holdout_r: str = "0.2",
) -> tuple[list[Position], dict[int, ExperimentOutcome], dict[int, Signal]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    positions = []
    outcomes = {}
    signals = {}
    for position_id in range(1, 61):
        value = Decimal(train_r if position_id <= 40 else holdout_r)
        positions.append(
            Position(
                id=position_id,
                market_id=1,
                mode="paper",
                side="long",
                entry_price=Decimal("100"),
                size=Decimal("1"),
                status="closed",
                opened_at=start + timedelta(hours=position_id),
                metadata_json={
                    "source_signal_id": position_id,
                    "experiment": {
                        "version": "paper-v2",
                        "assignment": "candidate",
                        "configuration_hash": "frozen-hash",
                    }
                },
            )
        )
        outcomes[position_id] = ExperimentOutcome(
            position_id=position_id,
            signal_id=position_id,
            market_id=1,
            experiment_version="paper-v2",
            configuration_hash="frozen-hash",
            assignment="candidate",
            timeframe="4h",
            side="long",
            barrier_label="test",
            opened_at=start + timedelta(hours=position_id),
            closed_at=start + timedelta(hours=position_id + 1),
            entry_price=Decimal("100"),
            net_r=value,
            metadata_json={"source_provider": "standard"},
        )
        signals[position_id] = Signal(
            id=position_id,
            market_id=1,
            timeframe="4h",
            direction="LONG",
            score=75,
            confidence_label="medium",
            entry_price=Decimal("100"),
            reason="test",
            strategy_breakdown={
                "_forward_gate": {
                    "expected_r_shadow_only": True,
                    "pre_tp1_shadow_only": True,
                }
            },
            status="new",
        )
    return positions, outcomes, signals


def test_cohort_requires_first_sixty_entries_and_all_their_outcomes() -> None:
    positions, outcomes, signals = _cohort()
    assert verify_forward_cohort(positions[:59], outcomes, signals)["status"] == "insufficient_entries"
    positions[10].status = "open"
    assert verify_forward_cohort(positions, outcomes, signals)["status"] == "pending_closed_trades"
    positions[10].status = "closed"
    del outcomes[11]
    assert verify_forward_cohort(positions, outcomes, signals)["status"] == "pending_outcomes"


def test_cohort_uses_entry_order_and_separate_net_holdout() -> None:
    positions, outcomes, signals = _cohort(holdout_r="-0.2")
    result = verify_forward_cohort(list(reversed(positions)), outcomes, signals, require_shadow_only=True)
    assert result["status"] == "criteria_not_met"
    assert result["train"]["criteria_met"] is True
    assert result["holdout"]["net_r"] == "-4.0"
    assert result["holdout"]["criteria_met"] is False


def test_later_winners_cannot_replace_early_losers() -> None:
    positions, outcomes, signals = _cohort(holdout_r="-0.2")
    later_positions, later_outcomes, later_signals = _cohort(holdout_r="1")
    later_positions = later_positions[40:]
    for position in later_positions:
        position.id += 100
        position.opened_at += timedelta(days=365)
    for outcome in later_outcomes.values():
        outcome.position_id += 100
        outcome.signal_id += 100
    for signal in later_signals.values():
        signal.id += 100
    outcomes.update({outcome.position_id: outcome for outcome in list(later_outcomes.values())[40:]})
    signals.update({signal.id: signal for signal in list(later_signals.values())[40:]})
    result = verify_forward_cohort(positions + later_positions, outcomes, signals)
    assert result["status"] == "criteria_not_met"
    assert result["first_cohort_entries"] == 60


def test_cohort_rejects_mixed_configuration_and_gate_phases() -> None:
    positions, outcomes, signals = _cohort()
    outcomes[41].configuration_hash = "different"
    positions[40].metadata_json["experiment"]["configuration_hash"] = "different"
    assert verify_forward_cohort(positions, outcomes, signals)["status"] == "mixed_configuration"
    outcomes[41].configuration_hash = "frozen-hash"
    positions[40].metadata_json["experiment"]["configuration_hash"] = "frozen-hash"
    signals[41].strategy_breakdown["_forward_gate"]["expected_r_shadow_only"] = False
    assert verify_forward_cohort(positions, outcomes, signals)["status"] == "mixed_gate_phases"
    early = verify_forward_cohort(positions[:41], outcomes, signals)
    assert early["status"] == "mixed_gate_phases"
    assert early["entries_observed"] == 41
    assert early["closed_entries"] == 41
    assert early["outcomes_available"] == 41


def test_cohort_requires_shadow_policy_and_attribution() -> None:
    positions, outcomes, signals = _cohort()
    assert verify_forward_cohort(positions, outcomes, signals, require_shadow_only=True)["status"] == "criteria_met"
    signals[41].strategy_breakdown["_forward_gate"] = {
        "expected_r_shadow_only": False,
        "pre_tp1_shadow_only": False,
    }
    for signal in signals.values():
        signal.strategy_breakdown["_forward_gate"] = signals[41].strategy_breakdown["_forward_gate"]
    assert verify_forward_cohort(positions, outcomes, signals, require_shadow_only=True)["status"] == "non_shadow_gate_phase"
    outcomes[41].metadata_json["source_provider"] = "the_bull"
    assert verify_forward_cohort(positions, outcomes, signals)["status"] == "non_standard_source_in_cohort"


def test_cohort_requires_the_declared_paper_fill_method() -> None:
    positions, outcomes, signals = _cohort()
    for position in positions:
        position.metadata_json["paper_fill_source"] = "fresh_l2_vwap"
    for outcome in outcomes.values():
        outcome.metadata_json["paper_fill_source"] = "fresh_l2_vwap"
    result = verify_forward_cohort(
        positions, outcomes, signals, required_paper_fill_source="fresh_l2_vwap"
    )
    assert result["status"] == "criteria_met"
    positions[0].metadata_json["paper_fill_source"] = "external_signal_plan"
    assert verify_forward_cohort(
        positions, outcomes, signals, required_paper_fill_source="fresh_l2_vwap"
    )["status"] == "invalid_paper_fill_source"


def test_exit_slippage_stress_must_pass_in_train_and_holdout() -> None:
    positions, outcomes, signals = _cohort(train_r="0.5", holdout_r="0.2")
    for position in positions:
        position.realized_pnl = Decimal("1")
        position.metadata_json["initial_position_risk"] = "1"
        position.metadata_json["planned_notional"] = "100"
    result = verify_forward_cohort(
        positions, outcomes, signals, exit_slippage_stress_pct=Decimal("0.3")
    )
    assert result["train"]["criteria_met"] is True
    assert result["holdout"]["criteria_met"] is True
    assert result["exit_stress"]["train"]["criteria_met"] is True
    assert result["exit_stress"]["holdout"]["criteria_met"] is False
    assert result["status"] == "criteria_not_met"
    positions[0].metadata_json.pop("planned_notional")
    assert verify_forward_cohort(
        positions, outcomes, signals, exit_slippage_stress_pct=Decimal("0.3")
    )["status"] == "missing_exit_stress_attribution"


def test_forward_holdout_excludes_entries_during_fixed_embargo() -> None:
    positions, outcomes, signals = _cohort()
    positions = positions[:5]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for position, hours in zip(positions, (1, 2, 3, 27, 28), strict=True):
        position.opened_at = start + timedelta(hours=hours)
        position.closed_at = position.opened_at + timedelta(hours=1)

    result = verify_forward_cohort(
        positions,
        outcomes,
        signals,
        train_count=2,
        holdout_count=2,
        train_holdout_embargo_hours=24,
    )
    assert result["status"] == "criteria_met"
    assert result["entries_observed"] == 5
    assert result["first_cohort_entries"] == 4
    assert result["entries_excluded_during_embargo"] == 1
    assert result["embargo_until"] == (start + timedelta(hours=26)).isoformat()
    positions[0].closed_at = start + timedelta(hours=28)
    assert verify_forward_cohort(
        positions,
        outcomes,
        signals,
        train_count=2,
        holdout_count=2,
        train_holdout_embargo_hours=24,
    )["status"] == "overlapping_train_holdout_positions"
    positions[0].metadata_json["paper_fill_source"] = "fresh_l2_vwap"
    outcomes[1].metadata_json["paper_fill_source"] = None
    assert verify_forward_cohort(
        positions, outcomes, signals, required_paper_fill_source="fresh_l2_vwap"
    )["status"] == "invalid_paper_fill_source"
