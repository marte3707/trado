from app.db.models import AppSetting, StrategyConfig
from app.engine.experiment import (
    experiment_snapshot,
    forward_gate_policy,
    neutralize_historical_overlays,
    paper_candidate_allows_signal,
    signal_experiment_tag,
)


class FakeDb:
    def __init__(self, settings: dict | None = None) -> None:
        self.setting = (
            AppSetting(key="research_experiment", value=settings)
            if settings is not None
            else None
        )

    def get(self, model: object, key: str) -> AppSetting | None:
        return self.setting if model is AppSetting and key == "research_experiment" else None


def strategy(name: str, weight: int) -> StrategyConfig:
    return StrategyConfig(
        strategy_name=name,
        timeframe="15m",
        enabled=True,
        params={"period": 21},
        weight=weight,
    )


def test_experiment_hash_is_stable_across_strategy_order() -> None:
    db = FakeDb({"active_version": "forward-v1"})
    risk = {"min_signal_score": 65, "min_risk_reward": 2.0}

    first = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("ema", 10), strategy("vwap", 8)],
        risk_value=risk,
    )
    second = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("vwap", 8), strategy("ema", 10)],
        risk_value=dict(reversed(list(risk.items()))),
    )

    assert first["version"] == "forward-v1"
    assert first["configuration_hash"] == second["configuration_hash"]


def test_experiment_hash_changes_when_weight_changes() -> None:
    db = FakeDb()

    first = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )
    second = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("ema", 11)],
        risk_value={"min_signal_score": 65},
    )

    assert first["configuration_hash"] != second["configuration_hash"]


def test_experiment_hash_changes_for_any_effective_risk_rule() -> None:
    db = FakeDb()

    first = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65, "trailing_activation_r": 1.0},
    )
    second = experiment_snapshot(
        db,  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65, "trailing_activation_r": 1.5},
    )

    assert first["configuration_hash"] != second["configuration_hash"]


def test_experiment_hash_separates_explicit_frozen_forward_gate_policy() -> None:
    legacy = experiment_snapshot(
        FakeDb(),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )
    frozen = experiment_snapshot(
        FakeDb({"forward_gates_shadow_only": True}),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )

    assert legacy["configuration_hash"] != frozen["configuration_hash"]


def test_experiment_hash_tracks_predeclared_exit_stress() -> None:
    common = {"required_paper_fill_source": "fresh_l2_vwap"}
    first = experiment_snapshot(
        FakeDb({**common, "exit_slippage_stress_pct": 0.1}),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )
    second = experiment_snapshot(
        FakeDb({**common, "exit_slippage_stress_pct": 0.2}),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )

    assert first["configuration_hash"] != second["configuration_hash"]


def test_experiment_hash_tracks_train_holdout_embargo() -> None:
    first = experiment_snapshot(
        FakeDb({"forward_train_holdout_embargo_hours": 168}),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )
    second = experiment_snapshot(
        FakeDb({"forward_train_holdout_embargo_hours": 96}),  # type: ignore[arg-type]
        strategies=[strategy("ema", 10)],
        risk_value={"min_signal_score": 65},
    )
    assert first["configuration_hash"] != second["configuration_hash"]


def test_signal_experiment_tag_returns_a_copy() -> None:
    breakdown = {"_experiment": {"version": "v1"}}

    tag = signal_experiment_tag(breakdown)

    assert tag == {"version": "v1"}
    assert tag is not breakdown["_experiment"]


class _CountQuery:
    def __init__(self, count: int) -> None:
        self.count = count

    def filter(self, *args: object) -> "_CountQuery":
        return self

    def scalar(self) -> int:
        return self.count


class GateDb(FakeDb):
    def __init__(self, settings: dict, count: int) -> None:
        super().__init__(settings)
        self.count = count

    def query(self, *args: object) -> _CountQuery:
        return _CountQuery(self.count)


class _RecordingCountQuery(_CountQuery):
    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.filters: list[object] = []

    def filter(self, *args: object) -> "_RecordingCountQuery":
        self.filters.extend(args)
        return self


class HashGateDb(GateDb):
    def __init__(self, settings: dict, count: int) -> None:
        super().__init__(settings, count)
        self.query_result = _RecordingCountQuery(count)

    def query(self, *args: object) -> _RecordingCountQuery:
        return self.query_result


def test_forward_gate_shadows_legacy_evidence_until_minimum_outcomes() -> None:
    db = GateDb(
        {
            "active_version": "baseline-frozen-v2",
            "assignment": "champion",
            "forward_gate_min_outcomes": 30,
        },
        count=29,
    )

    policy = forward_gate_policy(db)  # type: ignore[arg-type]

    assert policy["forward_ready"] is False
    assert policy["expected_r_shadow_only"] is True
    assert policy["pre_tp1_shadow_only"] is True
    assert policy["sample_source"] == "legacy_closed_positions"


def test_forward_gate_activates_only_active_experiment_outcomes() -> None:
    db = GateDb(
        {
            "active_version": "baseline-frozen-v2",
            "assignment": "champion",
            "forward_gate_min_outcomes": 30,
        },
        count=30,
    )

    policy = forward_gate_policy(db)  # type: ignore[arg-type]

    assert policy["forward_ready"] is True
    assert policy["expected_r_shadow_only"] is False
    assert policy["pre_tp1_shadow_only"] is False
    assert policy["sample_source"] == "experiment_outcomes"


def test_forward_gate_can_stay_shadow_for_a_frozen_paper_cohort() -> None:
    db = GateDb(
        {
            "active_version": "paper-high-score-v1",
            "assignment": "candidate_4h_high_score_long",
            "forward_gate_min_outcomes": 30,
            "forward_gates_shadow_only": True,
        },
        count=30,
    )

    policy = forward_gate_policy(db)  # type: ignore[arg-type]

    assert policy["forward_ready"] is True
    assert policy["sample_source"] == "experiment_outcomes"
    assert policy["forward_gates_shadow_only"] is True
    assert policy["expected_r_shadow_only"] is True
    assert policy["pre_tp1_shadow_only"] is True


def test_forward_gate_scopes_outcomes_to_the_effective_configuration_hash() -> None:
    db = HashGateDb(
        {
            "active_version": "baseline-frozen-v2",
            "assignment": "champion",
            "forward_gate_min_outcomes": 30,
        },
        count=30,
    )

    policy = forward_gate_policy(db, configuration_hash="current-hash")  # type: ignore[arg-type]

    assert policy["configuration_hash"] == "current-hash"
    assert any("configuration_hash" in str(expression) for expression in db.query_result.filters)


def test_paper_candidate_allows_only_its_declared_timeframe_and_direction() -> None:
    policy = {
        "paper_candidate": {
            "enabled": True,
            "name": "four_hour_long",
            "allowed_timeframes": ["4h"],
            "allowed_directions_by_timeframe": {"4h": ["LONG"]},
        }
    }

    allowed, allowed_info = paper_candidate_allows_signal(
        policy,
        timeframe="4h",
        direction="STRONG_LONG",
    )
    blocked_short, short_info = paper_candidate_allows_signal(
        policy,
        timeframe="4h",
        direction="SHORT",
    )
    blocked_timeframe, timeframe_info = paper_candidate_allows_signal(
        policy,
        timeframe="1h",
        direction="LONG",
    )

    assert allowed is True
    assert allowed_info["direction"] == "LONG"
    assert blocked_short is False
    assert short_info["reason"] == "direction_not_in_candidate"
    assert blocked_timeframe is False
    assert timeframe_info["reason"] == "timeframe_not_in_candidate"


def test_invalid_paper_candidate_fails_closed() -> None:
    allowed, info = paper_candidate_allows_signal(
        {"paper_candidate": {"enabled": True, "allowed_timeframes": []}},
        timeframe="4h",
        direction="LONG",
    )

    assert allowed is False
    assert info == {
        "active": True,
        "invalid": "allowed_timeframes",
        "reason": "invalid_candidate_configuration",
    }


def test_paper_candidate_min_score_is_paper_only_selection() -> None:
    policy = {
        "paper_candidate": {
            "enabled": True,
            "name": "four_hour_high_score_long",
            "allowed_timeframes": ["4h"],
            "allowed_directions_by_timeframe": {"4h": ["LONG"]},
            "min_score_by_timeframe": {"4h": 70},
        }
    }

    allowed, selected = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="STRONG_LONG", score=72
    )
    rejected, below = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="LONG", score=69
    )
    missing, no_score = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="LONG"
    )
    short, wrong_direction = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="SHORT", score=80
    )

    assert allowed is True
    assert selected["min_score"] == 70
    assert rejected is False
    assert below["reason"] == "score_below_candidate_minimum"
    assert missing is False
    assert no_score["reason"] == "score_below_candidate_minimum"
    assert short is False
    assert wrong_direction["reason"] == "direction_not_in_candidate"


def test_invalid_paper_candidate_min_score_fails_closed() -> None:
    policy = {
        "paper_candidate": {
            "enabled": True,
            "allowed_timeframes": ["4h"],
            "allowed_directions_by_timeframe": {"4h": ["LONG"]},
            "min_score_by_timeframe": {"4h": True},
        }
    }

    allowed, info = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="LONG", score=60
    )

    assert allowed is False
    assert info == {
        "active": True,
        "invalid": "min_score_by_timeframe",
        "reason": "invalid_candidate_configuration",
    }


def test_paper_candidate_requires_an_aligned_strategy_vote() -> None:
    policy = {
        "paper_candidate": {
            "enabled": True,
            "name": "wyckoff_long",
            "allowed_timeframes": ["4h"],
            "allowed_directions_by_timeframe": {"4h": ["LONG"]},
            "required_aligned_strategies": ["wyckoff_spring_upthrust"],
        }
    }
    allowed, selected = paper_candidate_allows_signal(
        policy,
        timeframe="4h",
        direction="LONG",
        score=54,
        aligned_strategies={"wyckoff_spring_upthrust", "ema_crossover"},
    )
    rejected, missing = paper_candidate_allows_signal(
        policy,
        timeframe="4h",
        direction="LONG",
        score=75,
        aligned_strategies={"ema_crossover"},
    )
    unknown, no_votes = paper_candidate_allows_signal(
        policy, timeframe="4h", direction="LONG", score=75
    )

    assert allowed is True
    assert selected["required_aligned_strategies"] == ["wyckoff_spring_upthrust"]
    assert rejected is False
    assert missing["reason"] == "required_strategy_not_aligned"
    assert unknown is False
    assert no_votes["reason"] == "required_strategy_not_aligned"


def test_paper_candidate_rejects_malformed_strategy_requirement() -> None:
    policy = {
        "paper_candidate": {
            "enabled": True,
            "allowed_timeframes": ["4h"],
            "allowed_directions_by_timeframe": {"4h": ["LONG"]},
            "required_aligned_strategies": [""],
        }
    }

    allowed, info = paper_candidate_allows_signal(
        policy,
        timeframe="4h",
        direction="LONG",
        score=75,
        aligned_strategies={"wyckoff_spring_upthrust"},
    )

    assert allowed is False
    assert info["reason"] == "invalid_candidate_configuration"
    assert info["invalid"] == "required_aligned_strategies"


def test_historical_overlays_are_neutralized_only_in_effective_copy() -> None:
    persisted = {
        "directional_min_score_adjustments": {"LONG:15m": {"adjustment": 35}},
        "direction_strategy_weight_overrides": {"15m:LONG:vwap": 1},
        "edge_penalties_enabled": True,
    }
    effective = dict(persisted)

    info = neutralize_historical_overlays(
        effective,
        {
            "historical_overlays_shadow_only": True,
            "version": "baseline-frozen-v2",
        },
    )

    assert effective["directional_min_score_adjustments"] == {}
    assert effective["direction_strategy_weight_overrides"] == {}
    assert effective["edge_penalties_enabled"] is False
    assert persisted["directional_min_score_adjustments"] == {
        "LONG:15m": {"adjustment": 35}
    }
    assert set(info["neutralized"]) == {
        "directional_min_score_adjustments",
        "direction_strategy_weight_overrides",
    }
