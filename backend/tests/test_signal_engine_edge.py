from datetime import UTC, datetime

from app.core.enums import Direction
from app.db.models import StrategyConfig
from app.engine.signal_engine import SignalEngine
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
