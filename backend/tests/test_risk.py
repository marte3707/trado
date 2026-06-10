from datetime import UTC, datetime

from app.core.enums import Direction
from app.engine.risk import RiskManager, calculate_position_size
from app.strategies.base import CandleInput


def build_market_candles(direction: Direction) -> list[CandleInput]:
    pattern = [0, 0.8, 0.2, 1.1, 0.4, 0.9]
    candles = []
    for index in range(80):
        if direction in {Direction.LONG, Direction.STRONG_LONG}:
            close = 100 + (index * 0.12) + (pattern[index % len(pattern)] * 0.8)
        else:
            close = 120 - (index * 0.12) - (pattern[index % len(pattern)] * 0.8)
        candles.append(
            CandleInput(
                datetime.now(UTC),
                close - 0.25,
                close + 0.7,
                close - 0.7,
                close,
                1000,
            )
        )
    return candles


def test_position_size_caps_by_leverage_notional() -> None:
    size = calculate_position_size(1000, 1, 100, 90, leverage=1)
    assert size == 1


def test_risk_plan_long_has_ordered_levels() -> None:
    candles = build_market_candles(Direction.LONG)
    plan = RiskManager().build_plan(candles, Direction.LONG, signal_score=78, min_risk_reward=2)
    assert plan is not None
    assert plan.stop_loss < plan.entry < plan.take_profit_1 < plan.take_profit_2
    assert plan.risk_reward >= 2


def test_risk_plan_short_has_ordered_levels() -> None:
    candles = build_market_candles(Direction.SHORT)
    plan = RiskManager().build_plan(candles, Direction.SHORT, signal_score=82, min_risk_reward=2)
    assert plan is not None
    assert plan.take_profit_2 < plan.take_profit_1 < plan.entry < plan.stop_loss
    assert plan.risk_reward >= 2


def test_risk_plan_uses_score_for_more_ambitious_targets() -> None:
    candles = build_market_candles(Direction.LONG)
    baseline = RiskManager().build_plan(candles, Direction.LONG, signal_score=60, min_risk_reward=2)
    strong = RiskManager().build_plan(candles, Direction.STRONG_LONG, signal_score=95, min_risk_reward=2)
    assert baseline is not None
    assert strong is not None
    assert strong.risk_reward > baseline.risk_reward


def test_higher_min_atr_multiplier_widens_stop_loss() -> None:
    candles = build_market_candles(Direction.LONG)
    baseline = RiskManager().build_plan(
        candles, Direction.LONG, signal_score=95, min_risk_reward=2, min_atr_multiplier=1.5
    )
    wider = RiskManager().build_plan(
        candles, Direction.LONG, signal_score=95, min_risk_reward=2, min_atr_multiplier=3.5
    )
    assert baseline is not None
    assert wider is not None
    # Stop più largo => prezzo dello stop più lontano dall'entry verso il basso
    assert wider.stop_loss < baseline.stop_loss


def test_default_min_atr_multiplier_preserves_legacy_behavior() -> None:
    candles = build_market_candles(Direction.LONG)
    legacy = RiskManager().build_plan(candles, Direction.LONG, signal_score=78, min_risk_reward=2)
    explicit_default = RiskManager().build_plan(
        candles,
        Direction.LONG,
        signal_score=78,
        min_risk_reward=2,
        min_atr_multiplier=1.5,
        min_stop_distance_pct=0.020,
    )
    assert legacy is not None
    assert explicit_default is not None
    assert legacy.stop_loss == explicit_default.stop_loss
    assert legacy.take_profit_2 == explicit_default.take_profit_2


def test_short_plan_clips_ambitious_target_before_it_turns_negative() -> None:
    pattern = [0, 0.8, 0.2, 1.1, 0.4, 0.9]
    candles = []
    for index in range(80):
        close = 0.6 - (index * 0.002) - (pattern[index % len(pattern)] * 0.02)
        candles.append(
            CandleInput(
                datetime.now(UTC),
                close - 0.002,
                close + 0.04,
                max(0.001, close - 0.04),
                close,
                1000,
            )
        )

    plan = RiskManager().build_plan(
        candles, Direction.STRONG_SHORT, signal_score=79, min_risk_reward=2
    )

    assert plan is not None
    assert plan.take_profit_2 > 0
    assert plan.take_profit_3 is None
    assert plan.take_profit_2 < plan.take_profit_1 < plan.entry < plan.stop_loss
    assert plan.risk_reward >= 2


def test_short_plan_is_rejected_when_minimum_target_would_be_negative() -> None:
    pattern = [0, 0.8, 0.2, 1.1, 0.4, 0.9]
    candles = []
    for index in range(80):
        close = 0.3 - (index * 0.001) - (pattern[index % len(pattern)] * 0.025)
        candles.append(
            CandleInput(
                datetime.now(UTC),
                close - 0.002,
                close + 0.05,
                max(0.001, close - 0.05),
                close,
                1000,
            )
        )

    assert (
        RiskManager().build_plan(
            candles, Direction.STRONG_SHORT, signal_score=79, min_risk_reward=2
        )
        is None
    )
