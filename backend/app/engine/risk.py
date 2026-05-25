from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.enums import Direction
from app.indicators.core import atr
from app.strategies.base import CandleInput

if TYPE_CHECKING:
    from app.engine.orderbook import OrderBookAnalysis

MIN_RISK_REWARD = 2.0
PIVOT_WINDOW = 4
STRUCTURE_LOOKBACK = 80
MIN_STOP_DISTANCE_PCT = 0.020
MAX_STOP_DISTANCE_PCT = 0.12


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float | None
    risk_reward: float


class RiskManager:
    def build_plan(
        self,
        candles: list[CandleInput],
        direction: Direction,
        signal_score: int = 60,
        min_risk_reward: float = MIN_RISK_REWARD,
        min_atr_multiplier: float = 1.5,
        min_stop_distance_pct: float = MIN_STOP_DISTANCE_PCT,
        ob_analysis: OrderBookAnalysis | None = None,
    ) -> RiskPlan | None:
        if len(candles) < 30 or direction == Direction.NEUTRAL:
            return None
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        latest_atr = atr(highs, lows, closes, 14)[-1]
        if latest_atr is None or latest_atr <= 0:
            return None
        entry = closes[-1]
        risk_reward_floor = max(MIN_RISK_REWARD, min_risk_reward)
        score_strength = _clamp((signal_score - 60) / 40, 0.0, 1.0)
        target_rr = risk_reward_floor + 0.5 + (0.75 * score_strength)
        stop_atr_multiplier = max(2.1 - (0.25 * score_strength), min_atr_multiplier)
        buffer = latest_atr * 0.30
        min_stop_distance = max(latest_atr * stop_atr_multiplier, entry * min_stop_distance_pct)
        max_stop_distance = max(entry * MAX_STOP_DISTANCE_PCT, latest_atr * 4.0)
        support_levels, resistance_levels = _structure_levels(candles[-STRUCTURE_LOOKBACK:])
        if direction in {Direction.LONG, Direction.STRONG_LONG}:
            support = _nearest_below(support_levels, entry) or min(lows[-20:])
            swing_stop = min(lows[-20:]) - buffer
            atr_stop = entry - min_stop_distance
            stop = min(support - buffer, swing_stop, atr_stop)
            risk = entry - stop
            if stop <= 0 or risk <= 0 or risk > max_stop_distance:
                return None
            tp1_target = entry + risk
            tp2_target = entry + (risk * target_rr)
            tp3_target = entry + risk * (target_rr + 1)
            tp1 = _target_before_resistance(resistance_levels, tp1_target, buffer)
            tp2 = _target_before_resistance(resistance_levels, tp2_target, buffer)
            tp3: float | None = _target_before_resistance(resistance_levels, tp3_target, buffer)
            tp1 = max(tp1, entry + risk)
            tp2 = max(tp2, entry + risk * risk_reward_floor)
            if tp2 <= tp1:
                tp2 = entry + risk * max(risk_reward_floor, 2.0)
            if tp3 is not None and tp3 <= tp2:
                tp3 = None
            if ob_analysis is not None:
                wall_buf = latest_atr * 0.15
                tp1 = _adjust_tp_for_walls(direction, entry, tp1, ob_analysis.ask_walls, wall_buf)
                tp2 = _adjust_tp_for_walls(direction, entry, tp2, ob_analysis.ask_walls, wall_buf)
                if tp3 is not None:
                    tp3 = _adjust_tp_for_walls(direction, entry, tp3, ob_analysis.ask_walls, wall_buf)
        else:
            resistance = _nearest_above(resistance_levels, entry) or max(highs[-20:])
            swing_stop = max(highs[-20:]) + buffer
            atr_stop = entry + min_stop_distance
            stop = max(resistance + buffer, swing_stop, atr_stop)
            risk = stop - entry
            if risk <= 0 or risk > max_stop_distance:
                return None
            tp1_target = entry - risk
            tp2_target = entry - (risk * target_rr)
            tp3_target = entry - risk * (target_rr + 1)
            floor_target = entry - risk * risk_reward_floor
            if floor_target <= 0:
                return None
            if tp2_target <= 0:
                tp2_target = floor_target
            if tp3_target <= 0:
                tp3_target = None
            tp1 = _target_before_support(support_levels, tp1_target, buffer)
            tp2 = _target_before_support(support_levels, tp2_target, buffer)
            tp3 = (
                _target_before_support(support_levels, tp3_target, buffer)
                if tp3_target is not None
                else None
            )
            tp1 = min(tp1, entry - risk)
            tp2 = min(tp2, entry - risk * risk_reward_floor)
            if tp2 >= tp1:
                tp2 = entry - risk * max(risk_reward_floor, 2.0)
            if tp2 <= 0:
                return None
            if tp3 is not None and (tp3 >= tp2 or tp3 <= 0):
                tp3 = None
            if ob_analysis is not None:
                wall_buf = latest_atr * 0.15
                tp1 = _adjust_tp_for_walls(direction, entry, tp1, ob_analysis.bid_walls, wall_buf)
                tp2 = _adjust_tp_for_walls(direction, entry, tp2, ob_analysis.bid_walls, wall_buf)
                if tp3 is not None:
                    tp3 = _adjust_tp_for_walls(direction, entry, tp3, ob_analysis.bid_walls, wall_buf)
        if direction in {Direction.LONG, Direction.STRONG_LONG}:
            if tp3 is not None and tp3 <= tp2:
                tp3 = None
        elif tp3 is not None and (tp3 >= tp2 or tp3 <= 0):
            tp3 = None
        if not _levels_are_valid(direction, entry, stop, tp1, tp2, tp3):
            return None
        actual_risk_reward = abs(tp2 - entry) / risk
        if actual_risk_reward < risk_reward_floor:
            return None
        return RiskPlan(
            entry=entry,
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward=actual_risk_reward,
        )


def calculate_position_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    leverage: float = 1.0,
) -> float:
    risk_amount = equity * (risk_pct / 100)
    per_unit_risk = abs(entry_price - stop_loss)
    if equity <= 0 or risk_amount <= 0 or per_unit_risk <= 0 or leverage <= 0:
        return 0.0
    raw_size = risk_amount / per_unit_risk
    max_notional_size = (equity * leverage) / entry_price
    return max(0.0, min(raw_size, max_notional_size))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _structure_levels(candles: list[CandleInput]) -> tuple[list[float], list[float]]:
    supports: list[float] = []
    resistances: list[float] = []
    if len(candles) < (PIVOT_WINDOW * 2) + 1:
        return supports, resistances
    for index in range(PIVOT_WINDOW, len(candles) - PIVOT_WINDOW):
        left = candles[index - PIVOT_WINDOW : index]
        right = candles[index + 1 : index + PIVOT_WINDOW + 1]
        candle = candles[index]
        if all(candle.low < item.low for item in [*left, *right]):
            supports.append(candle.low)
        if all(candle.high > item.high for item in [*left, *right]):
            resistances.append(candle.high)
    supports.append(min(candle.low for candle in candles[-20:]))
    resistances.append(max(candle.high for candle in candles[-20:]))
    return sorted(set(supports)), sorted(set(resistances))


def _nearest_below(levels: list[float], value: float) -> float | None:
    candidates = [level for level in levels if level < value]
    return max(candidates) if candidates else None


def _nearest_above(levels: list[float], value: float) -> float | None:
    candidates = [level for level in levels if level > value]
    return min(candidates) if candidates else None


def _first_level_at_or_beyond(levels: list[float], value: float) -> float | None:
    candidates = [level for level in levels if level >= value]
    return min(candidates) if candidates else None


def _first_level_at_or_below(levels: list[float], value: float) -> float | None:
    candidates = [level for level in levels if level <= value]
    return max(candidates) if candidates else None


def _target_before_resistance(levels: list[float], target: float, buffer: float) -> float:
    resistance = _first_level_at_or_beyond(levels, target + buffer)
    return resistance - buffer if resistance is not None else target


def _target_before_support(levels: list[float], target: float, buffer: float) -> float:
    support = _first_level_at_or_below(levels, target - buffer)
    return support + buffer if support is not None else target


def _adjust_tp_for_walls(
    direction: Direction,
    entry: float,
    tp: float,
    walls: list,  # list[WallLevel] — usa stringa per evitare import circolare
    buffer: float,
) -> float:
    """Sposta il TP appena prima di un muro OB che blocca il percorso verso il target."""
    if direction in {Direction.LONG, Direction.STRONG_LONG}:
        blocking = [w for w in walls if entry < w.price < tp]
        if blocking:
            nearest = min(blocking, key=lambda w: w.price)
            adjusted = nearest.price - buffer
            return max(adjusted, entry)
    else:
        blocking = [w for w in walls if tp < w.price < entry]
        if blocking:
            nearest = max(blocking, key=lambda w: w.price)
            adjusted = nearest.price + buffer
            return min(adjusted, entry)
    return tp


def _levels_are_valid(
    direction: Direction,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    tp3: float | None,
) -> bool:
    if min(entry, stop, tp1, tp2) <= 0:
        return False
    if direction in {Direction.LONG, Direction.STRONG_LONG}:
        return stop < entry < tp1 < tp2 and (tp3 is None or tp2 < tp3)
    return tp2 < tp1 < entry < stop and (tp3 is None or tp3 < tp2)
