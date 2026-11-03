from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.enums import Direction
from app.db.models import DecisionLog, StrategyConfig
from app.engine.scoring import ScoreResult
from app.engine.signal_engine import (
    SignalEngine,
    source_candle_lag_minutes,
)
from app.strategies.base import CandleInput


def _candles(closes: list[float]) -> list[CandleInput]:
    return [
        CandleInput(
            open_time=datetime(2026, 1, 1, tzinfo=UTC),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000,
            open_interest=None,
        )
        for close in closes
    ]


def test_source_candle_lag_is_measured_after_expected_close() -> None:
    now = datetime(2026, 7, 20, 12, 20, tzinfo=UTC)

    assert source_candle_lag_minutes(
        "15m",
        datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        now=now,
    ) == 5
    assert source_candle_lag_minutes(
        "1h",
        datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        now=now,
    ) == 0


def test_entry_quality_long_accepts_close_in_upper_half() -> None:
    candles = _candles([100.0] * 25)
    candles[-1] = CandleInput(
        open_time=datetime(2026, 1, 1, tzinfo=UTC),
        open=100,
        high=106,
        low=98,
        close=104.5,
        volume=1000,
        open_interest=None,
    )

    passed, info = SignalEngine._entry_quality_result(candles, Direction.LONG, {}, None)

    assert passed is True
    assert info["confirmations"]["close_location"] is True


def test_entry_quality_long_rejects_low_close_below_ema_without_orderbook() -> None:
    candles = _candles([100.0] * 25)
    candles[-1] = CandleInput(
        open_time=datetime(2026, 1, 1, tzinfo=UTC),
        open=100,
        high=102,
        low=90,
        close=94,
        volume=1000,
        open_interest=None,
    )

    passed, info = SignalEngine._entry_quality_result(candles, Direction.LONG, {}, None)

    assert passed is False
    assert info["confirmations"] == {
        "close_location": False,
        "ema": False,
        "orderbook": False,
    }


def test_ob_imbalance_only_signal_is_blocked_by_guardrail() -> None:
    breakdown = {
        "ob_imbalance": {
            "direction": "SHORT",
            "contribution": 5.6,
        },
        "_score": {"short_normalized": 70},
    }

    assert SignalEngine._is_ob_imbalance_only_signal(breakdown, Direction.SHORT) is True


def test_multi_source_signal_is_not_flagged_as_ob_imbalance_only() -> None:
    breakdown = {
        "ob_imbalance": {
            "direction": "SHORT",
            "contribution": 5.6,
        },
        "cvd_divergence": {
            "direction": "SHORT",
            "contribution": 7.1,
        },
        "_score": {"short_normalized": 73},
    }

    assert SignalEngine._is_ob_imbalance_only_signal(breakdown, Direction.SHORT) is False


def test_disabled_strategy_names_for_timeframe_filters_only_target_timeframe() -> None:
    risk_value = {
        "disabled_strategies_by_timeframe": {
            "5m": ["cvd_divergence"],
            "15m": ["volume_profile"],
        }
    }

    disabled_5m = SignalEngine._disabled_strategy_names_for_timeframe(risk_value, "5m")
    disabled_15m = SignalEngine._disabled_strategy_names_for_timeframe(risk_value, "15m")

    assert disabled_5m == {"cvd_divergence"}
    assert disabled_15m == {"volume_profile"}


def test_disabled_strategy_names_for_timeframe_ignores_unknown_or_invalid_entries() -> None:
    risk_value = {
        "disabled_strategies_by_timeframe": {
            "5m": ["cvd_divergence", "", 123],
            "1h": "volume_profile",
        }
    }

    disabled_5m = SignalEngine._disabled_strategy_names_for_timeframe(risk_value, "5m")
    disabled_15m = SignalEngine._disabled_strategy_names_for_timeframe(risk_value, "15m")

    assert disabled_5m == {"cvd_divergence"}
    assert disabled_15m == set()


def test_enabled_strategy_names_for_timeframe_filters_only_target_timeframe() -> None:
    risk_value = {
        "enabled_strategies_by_timeframe": {
            "5m": ["trend_following_adaptive"],
            "15m": ["volume_profile"],
        }
    }

    enabled_5m = SignalEngine._enabled_strategy_names_for_timeframe(risk_value, "5m")
    enabled_15m = SignalEngine._enabled_strategy_names_for_timeframe(risk_value, "15m")

    assert enabled_5m == {"trend_following_adaptive"}
    assert enabled_15m == {"volume_profile"}


def test_enabled_strategy_names_for_timeframe_ignores_unknown_or_invalid_entries() -> None:
    risk_value = {
        "enabled_strategies_by_timeframe": {
            "5m": ["trend_following_adaptive", "", 123],
            "1h": "volume_profile",
        }
    }

    enabled_5m = SignalEngine._enabled_strategy_names_for_timeframe(risk_value, "5m")
    enabled_15m = SignalEngine._enabled_strategy_names_for_timeframe(risk_value, "15m")

    assert enabled_5m == {"trend_following_adaptive"}
    assert enabled_15m == set()


def test_strategy_config_filter_leaves_other_internal_strategies_untouched() -> None:
    configs = [
        StrategyConfig(strategy_name="cvd_divergence", timeframe="5m", enabled=True, params={}, weight=10),
        StrategyConfig(strategy_name="volume_profile", timeframe="5m", enabled=True, params={}, weight=10),
        StrategyConfig(strategy_name="ema_crossover", timeframe="5m", enabled=True, params={}, weight=10),
        StrategyConfig(strategy_name="cvd_divergence", timeframe="15m", enabled=True, params={}, weight=10),
    ]
    disabled = SignalEngine._disabled_strategy_names_for_timeframe(
        {"disabled_strategies_by_timeframe": {"5m": ["cvd_divergence", "unknown_strategy"]}},
        "5m",
    )

    filtered_5m = [config.strategy_name for config in configs[:3] if config.strategy_name not in disabled]
    filtered_15m = [
        config.strategy_name
        for config in configs[3:]
        if config.strategy_name not in SignalEngine._disabled_strategy_names_for_timeframe(
            {"disabled_strategies_by_timeframe": {"5m": ["cvd_divergence"]}},
            "15m",
        )
    ]

    assert filtered_5m == ["volume_profile", "ema_crossover"]
    assert filtered_15m == ["cvd_divergence"]


def test_timeframe_min_score_override_raises_threshold() -> None:
    from app.engine.signal_engine import apply_timeframe_min_score_override

    risk_value = {"timeframe_min_score_overrides": {"5m": 70}}

    assert apply_timeframe_min_score_override(risk_value, "5m", 65) == 70
    assert apply_timeframe_min_score_override(risk_value, "15m", 65) == 65


def test_timeframe_min_score_override_never_lowers_threshold() -> None:
    from app.engine.signal_engine import apply_timeframe_min_score_override

    risk_value = {"timeframe_min_score_overrides": {"5m": 50}}

    assert apply_timeframe_min_score_override(risk_value, "5m", 65) == 65


def test_timeframe_min_score_override_ignores_invalid_values() -> None:
    from app.engine.signal_engine import apply_timeframe_min_score_override

    assert apply_timeframe_min_score_override({}, "5m", 65) == 65
    assert (
        apply_timeframe_min_score_override(
            {"timeframe_min_score_overrides": "not-a-dict"}, "5m", 65
        )
        == 65
    )
    assert (
        apply_timeframe_min_score_override(
            {"timeframe_min_score_overrides": {"5m": "abc"}}, "5m", 65
        )
        == 65
    )


def test_edge_penalty_switch_suspends_overlay_but_preserves_runtime_evidence() -> None:
    from app.engine.signal_engine import effective_edge_adjustment

    risk = {
        "edge_penalties_enabled": False,
        "max_combined_score_adjustment": 15,
        "directional_score_adjustments": {"LONG:15m": {"adjustment": 8}},
    }
    enabled, adjustment = effective_edge_adjustment(
        risk,
        market_id=1,
        timeframe="15m",
        direction=Direction.LONG,
        score=68,
        suspended_family=None,
    )

    assert enabled is False
    assert adjustment["total"] == 0
    assert risk["directional_score_adjustments"] == {"LONG:15m": {"adjustment": 8}}


def test_volatility_shadow_sampling_respects_config_and_cycle_cap() -> None:
    engine = SignalEngine()
    source_time = datetime(2026, 1, 1, tzinfo=UTC)

    assert (
        engine._should_sample_volatility_skip(
            {"volatility_filter_shadow_sample_enabled": False},
            market_id=1,
            timeframe="5m",
            source_candle_open_time=source_time,
        )
        is False
    )
    assert (
        engine._should_sample_volatility_skip(
            {"volatility_filter_shadow_sample_rate": 0},
            market_id=1,
            timeframe="5m",
            source_candle_open_time=source_time,
        )
        is False
    )
    assert engine._should_sample_volatility_skip(
        {
            "volatility_filter_shadow_sample_rate": 1,
            "volatility_filter_shadow_max_per_cycle": 1,
        },
        market_id=1,
        timeframe="5m",
        source_candle_open_time=source_time,
    ) is True

    engine._cycle_volatility_shadow_samples = 1
    assert (
        engine._should_sample_volatility_skip(
            {
                "volatility_filter_shadow_sample_rate": 1,
                "volatility_filter_shadow_max_per_cycle": 1,
            },
            market_id=1,
            timeframe="5m",
            source_candle_open_time=source_time,
        )
        is False
    )


def test_volatility_shadow_sampling_invalid_config_falls_back_safely() -> None:
    engine = SignalEngine()

    result = engine._should_sample_volatility_skip(
        {
            "volatility_filter_shadow_sample_rate": "bad",
            "volatility_filter_shadow_max_per_cycle": "bad",
        },
        market_id=1,
        timeframe="15m",
        source_candle_open_time=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert isinstance(result, bool)


def test_candidate_rejection_sampling_prioritizes_near_misses_and_respects_cap() -> None:
    engine = SignalEngine()
    source_time = datetime(2026, 1, 1, tzinfo=UTC)
    risk = {
        "signal_candidate_diagnostics_sample_rate": 0,
        "signal_candidate_diagnostics_max_per_cycle": 1,
        "signal_candidate_diagnostics_near_miss_delta": 15,
    }

    assert engine._should_sample_candidate_rejection(
        risk,
        market_id=1,
        timeframe="15m",
        source_candle_open_time=source_time,
        score=55,
        effective_min_score=65,
    ) is True

    engine._cycle_candidate_rejection_samples = 1
    assert engine._should_sample_candidate_rejection(
        risk,
        market_id=1,
        timeframe="15m",
        source_candle_open_time=source_time,
        score=55,
        effective_min_score=65,
    ) is False


def test_candidate_rejection_sample_records_readable_threshold_pipeline() -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.rows: list[DecisionLog] = []

        def add(self, row: DecisionLog) -> None:
            self.rows.append(row)

    db = FakeDb()
    engine = SignalEngine()
    score = ScoreResult(
        direction=Direction.LONG,
        score=68,
        confidence_label="buono",
        reason="test",
        breakdown={"_score": {"voting_weight": 29, "all_weight": 63}},
    )
    engine._record_candidate_rejection_sample(
        db,  # type: ignore[arg-type]
        market=SimpleNamespace(id=1, symbol="HYPE"),  # type: ignore[arg-type]
        timeframe="15m",
        source_candle_open_time=datetime(2026, 1, 1, tzinfo=UTC),
        risk_value={"signal_candidate_diagnostics_sample_rate": 1},
        score=score,
        effective_min_score=73,
        skip_reason="directional_score_filter",
        participation_ratio=0.45,
        regime_info={"regime": "trending", "neutralized": []},
        shadow_score=None,
        shadow_participation_ratio=None,
        base_min_score=65,
        edge_adjustment={
            "total": 8,
            "raw_total": 16,
            "components": {"directional": 8, "score_bucket": 8},
        },
        edge_penalties_enabled=True,
    )

    assert len(db.rows) == 1
    assert db.rows[0].data["threshold_pipeline"] == {
        "base_min_score": 65,
        "edge_penalties_enabled": True,
        "edge_total": 8,
        "edge_raw_total": 16,
        "edge_components": {"directional": 8, "score_bucket": 8},
        "final_min_score": 73,
    }
