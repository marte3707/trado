from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AppSetting, DecisionLog, StrategyConfig
from app.engine.performance import PerformanceStats, SignalPerformanceAnalyzer, serialize_stats


@dataclass(frozen=True)
class CalibrationChange:
    target: str
    key: str
    before: float | int | dict[str, Any] | None
    after: float | int | dict[str, Any]
    reason: str


@dataclass(frozen=True)
class CalibrationResult:
    applied: bool
    eligible: bool
    reason: str
    overall: PerformanceStats
    changes: list[CalibrationChange]
    strategy_adjustments: dict[str, dict[str, Any]]
    settings: dict[str, Any]


class AutoCalibrationEngine:
    def __init__(self, analyzer: SignalPerformanceAnalyzer | None = None) -> None:
        self.analyzer = analyzer or SignalPerformanceAnalyzer()

    def run(self, db: Session, *, force: bool = False, commit: bool = True) -> CalibrationResult:
        settings = self._settings(db)
        positions = self.analyzer.closed_paper_positions(
            db,
            limit=int(settings["lookback_trades"]),
        )
        overall = self.analyzer.summarize_positions(positions)
        if not settings["enabled"] and not force:
            return self._result(False, False, "auto calibration disabled", overall, [], {}, settings)
        if overall.trades < int(settings["min_closed_trades"]):
            return self._result(
                False,
                False,
                "not enough closed trades",
                overall,
                [],
                {},
                settings,
            )

        changes: list[CalibrationChange] = []
        changes.extend(self._calibrate_strategy_weights(db, settings))
        changes.extend(self._calibrate_strategy_params(db, settings))
        changes.extend(self._calibrate_risk_settings(db, overall, settings))
        strategy_changes = {
            change.key: {
                "target": change.target,
                "before": change.before,
                "after": change.after,
                "reason": change.reason,
            }
            for change in changes
            if change.target in {"strategy_weight", "strategy_params"}
        }

        if commit:
            state = db.get(AppSetting, "auto_calibration_state") or AppSetting(
                key="auto_calibration_state",
                value={},
            )
            state.value = {
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "last_trade_count": overall.trades,
                "last_summary": serialize_stats(overall),
                "last_changes": [change.__dict__ for change in changes],
            }
            db.add(state)
            db.add(
                DecisionLog(
                    event_type="auto_calibration_applied" if changes else "auto_calibration_checked",
                    message="Auto calibration completed",
                    data={
                        "force": force,
                        "overall": serialize_stats(overall),
                        "changes": [change.__dict__ for change in changes],
                    },
                )
            )
            db.commit()
        return self._result(bool(changes), True, "calibration completed", overall, changes, strategy_changes, settings)

    def preview(self, db: Session) -> CalibrationResult:
        return self.run(db, force=True, commit=False)

    def _settings(self, db: Session) -> dict[str, Any]:
        current = db.get(AppSetting, "auto_calibration")
        value = dict(current.value if current and isinstance(current.value, dict) else {})
        defaults = {
            "enabled": True,
            "lookback_trades": 500,
            "min_closed_trades": 20,
            "min_strategy_trades": 8,
            "min_weight": 1,
            "max_weight": 30,
            "min_signal_score_floor": 55,
            "min_signal_score_ceiling": 80,
            "min_risk_reward_floor": 2.0,
            "min_risk_reward_ceiling": 3.0,
            "min_risk_per_trade_pct": 0.25,
        }
        return {**defaults, **value}

    def _baseline_state(self, db: Session) -> dict[str, Any]:
        state = db.get(AppSetting, "auto_calibration_baseline")
        if state is not None and isinstance(state.value, dict):
            return dict(state.value)

        settings = get_settings()
        risk_setting = db.get(AppSetting, "risk")
        risk = dict(risk_setting.value if risk_setting and isinstance(risk_setting.value, dict) else {})
        strategies = db.query(StrategyConfig).all()
        baseline = {
            "risk": {
                "min_signal_score": int(risk.get("min_signal_score", settings.min_signal_score)),
                "min_risk_reward": float(risk.get("min_risk_reward", max(settings.min_risk_reward, 2.0))),
                "max_risk_per_trade_pct": float(
                    risk.get("max_risk_per_trade_pct", settings.max_risk_per_trade_pct)
                ),
            },
            "strategy_weights": {
                f"{strategy.timeframe}:{strategy.strategy_name}": int(strategy.weight)
                for strategy in strategies
            },
            "strategy_params": {
                f"{strategy.timeframe}:{strategy.strategy_name}": dict(strategy.params or {})
                for strategy in strategies
            },
        }
        db.add(AppSetting(key="auto_calibration_baseline", value=baseline))
        return baseline

    def _baseline_strategy_params(self, db: Session) -> dict[str, dict[str, Any]]:
        baseline = self._baseline_state(db)
        if isinstance(baseline.get("strategy_params"), dict):
            return baseline["strategy_params"]

        strategies = db.query(StrategyConfig).all()
        strategy_params = {
            f"{strategy.timeframe}:{strategy.strategy_name}": dict(strategy.params or {})
            for strategy in strategies
        }
        baseline["strategy_params"] = strategy_params
        state = db.get(AppSetting, "auto_calibration_baseline")
        if state is not None:
            state.value = baseline
            db.add(state)
        return strategy_params

    def _calibrate_strategy_weights(self, db: Session, settings: dict[str, Any]) -> list[CalibrationChange]:
        baseline = self._baseline_state(db)
        baseline_weights: dict[str, int] = baseline.get("strategy_weights", {})
        min_trades = int(settings["min_strategy_trades"])
        min_weight = int(settings["min_weight"])
        max_weight = int(settings["max_weight"])
        changes: list[CalibrationChange] = []
        strategies = db.query(StrategyConfig).all()
        stats_by_timeframe: dict[str, dict[str, PerformanceStats]] = {}

        for strategy in strategies:
            if strategy.timeframe not in stats_by_timeframe:
                stats_by_timeframe[strategy.timeframe] = self.analyzer.strategy_stats(
                    db,
                    timeframe=strategy.timeframe,
                    limit=int(settings["lookback_trades"]),
                )
            stats_by_strategy = stats_by_timeframe[strategy.timeframe]
            stats = stats_by_strategy.get(strategy.strategy_name)
            if stats is None or stats.trades < min_trades:
                continue
            key = f"{strategy.timeframe}:{strategy.strategy_name}"
            baseline_weight = int(baseline_weights.get(key, strategy.weight))
            factor, reason = self._strategy_factor(stats)
            calibrated = max(min_weight, min(max_weight, round(baseline_weight * factor)))
            if calibrated == strategy.weight:
                continue
            changes.append(
                CalibrationChange(
                    target="strategy_weight",
                    key=key,
                    before=strategy.weight,
                    after=calibrated,
                    reason=reason,
                )
            )
            strategy.weight = calibrated
        return changes

    def _calibrate_strategy_params(self, db: Session, settings: dict[str, Any]) -> list[CalibrationChange]:
        baseline_params = self._baseline_strategy_params(db)
        min_trades = int(settings["min_strategy_trades"])
        changes: list[CalibrationChange] = []
        strategies = db.query(StrategyConfig).all()
        stats_by_timeframe: dict[str, dict[str, PerformanceStats]] = {}

        for strategy in strategies:
            if strategy.timeframe not in stats_by_timeframe:
                stats_by_timeframe[strategy.timeframe] = self.analyzer.strategy_stats(
                    db,
                    timeframe=strategy.timeframe,
                    limit=int(settings["lookback_trades"]),
                )
            stats = stats_by_timeframe[strategy.timeframe].get(strategy.strategy_name)
            if stats is None or stats.trades < min_trades:
                continue

            key = f"{strategy.timeframe}:{strategy.strategy_name}"
            base_params = dict(baseline_params.get(key, strategy.params or {}))
            next_params, reason = self._strategy_params_for_stats(strategy.strategy_name, base_params, stats)
            current_params = dict(strategy.params or {})
            if next_params == current_params:
                continue
            changes.append(
                CalibrationChange(
                    target="strategy_params",
                    key=key,
                    before=current_params,
                    after=next_params,
                    reason=reason,
                )
            )
            strategy.params = next_params
        return changes

    def _calibrate_risk_settings(
        self,
        db: Session,
        overall: PerformanceStats,
        settings: dict[str, Any],
    ) -> list[CalibrationChange]:
        baseline = self._baseline_state(db).get("risk", {})
        app_settings = get_settings()
        risk_setting = db.get(AppSetting, "risk") or AppSetting(key="risk", value={})
        risk = dict(risk_setting.value if isinstance(risk_setting.value, dict) else {})

        base_score = int(baseline.get("min_signal_score", app_settings.min_signal_score))
        base_rr = float(baseline.get("min_risk_reward", max(app_settings.min_risk_reward, 2.0)))
        base_risk = float(baseline.get("max_risk_per_trade_pct", app_settings.max_risk_per_trade_pct))

        score_delta = 0
        rr_delta = 0.0
        risk_pct = base_risk
        reason = "historical performance near baseline"
        if overall.realized_pnl < 0 or overall.win_rate < 0.4 or (
            overall.profit_factor is not None and overall.profit_factor < 0.9
        ):
            score_delta = 8
            rr_delta = 0.25
            risk_pct = max(float(settings["min_risk_per_trade_pct"]), base_risk * 0.75)
            reason = "defensive mode after weak historical results"
        elif overall.win_rate >= 0.58 and overall.realized_pnl > 0 and (
            overall.profit_factor is None or overall.profit_factor >= 1.2
        ):
            score_delta = 0
            rr_delta = 0.0
            risk_pct = base_risk
            reason = "restore baseline after strong historical results"

        next_score = int(
            max(
                int(settings["min_signal_score_floor"]),
                min(int(settings["min_signal_score_ceiling"]), base_score + score_delta),
            )
        )
        next_rr = round(
            max(
                float(settings["min_risk_reward_floor"]),
                min(float(settings["min_risk_reward_ceiling"]), base_rr + rr_delta),
            ),
            2,
        )
        next_risk_pct = round(min(base_risk, risk_pct), 4)

        changes: list[CalibrationChange] = []
        for key, value in {
            "min_signal_score": next_score,
            "min_risk_reward": next_rr,
            "max_risk_per_trade_pct": next_risk_pct,
        }.items():
            before = risk.get(key)
            if before == value:
                continue
            risk[key] = value
            changes.append(
                CalibrationChange(
                    target="risk_setting",
                    key=key,
                    before=before,
                    after=value,
                    reason=reason,
                )
            )
        risk_setting.value = risk
        db.add(risk_setting)
        return changes

    def _strategy_factor(self, stats: PerformanceStats) -> tuple[float, str]:
        profit_factor = stats.profit_factor
        if stats.realized_pnl < 0 or stats.win_rate < 0.35 or (
            profit_factor is not None and profit_factor < 0.8
        ):
            return 0.6, "strategy underperformed historically"
        if stats.win_rate < 0.45 or (profit_factor is not None and profit_factor < 1.0):
            return 0.8, "strategy has below-target historical performance"
        if stats.win_rate >= 0.6 and stats.realized_pnl > 0 and (
            profit_factor is None or profit_factor >= 1.25
        ):
            return 1.2, "strategy outperformed historically"
        if stats.win_rate >= 0.55 and stats.realized_pnl > 0:
            return 1.1, "strategy has positive historical edge"
        return 1.0, "strategy performance near baseline"

    def _strategy_params_for_stats(
        self,
        strategy_name: str,
        baseline_params: dict[str, Any],
        stats: PerformanceStats,
    ) -> tuple[dict[str, Any], str]:
        if not baseline_params:
            return baseline_params, "strategy has no tunable parameters"
        if stats.realized_pnl < 0 or stats.win_rate < 0.35 or (
            stats.profit_factor is not None and stats.profit_factor < 0.8
        ):
            return (
                self._defensive_strategy_params(strategy_name, baseline_params, intensity=1.0),
                "strategy underperformed historically; parameters made more selective",
            )
        if stats.win_rate < 0.45 or (stats.profit_factor is not None and stats.profit_factor < 1.0):
            return (
                self._defensive_strategy_params(strategy_name, baseline_params, intensity=0.5),
                "strategy has below-target historical performance; parameters made slightly more selective",
            )
        if stats.win_rate >= 0.55 and stats.realized_pnl > 0 and (
            stats.profit_factor is None or stats.profit_factor >= 1.1
        ):
            return dict(baseline_params), "strategy has recovered; parameters restored to baseline"
        return dict(baseline_params), "strategy performance near baseline"

    def _defensive_strategy_params(
        self,
        strategy_name: str,
        baseline_params: dict[str, Any],
        *,
        intensity: float,
    ) -> dict[str, Any]:
        params = dict(baseline_params)

        def number(key: str, fallback: float) -> float:
            try:
                return float(params.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def rounded(value: float, step: float = 1.0) -> float | int:
            scaled = round(value / step) * step
            return int(scaled) if step == 1.0 else round(scaled, 6)

        def clamp(value: float, low: float, high: float) -> float:
            return max(low, min(high, value))

        def set_int(key: str, factor: float, low: int = 2, high: int = 500) -> None:
            if key in params:
                params[key] = int(rounded(clamp(number(key, 0) * factor, low, high)))

        def set_float(key: str, factor: float, low: float, high: float, step: float = 0.0001) -> None:
            if key in params:
                params[key] = rounded(clamp(number(key, 0.0) * factor, low, high), step)

        slow_factor = 1.0 + (0.2 * intensity)
        strict_factor = 1.0 + (0.25 * intensity)
        tolerance_factor = 1.0 - (0.25 * intensity)

        if strategy_name == "ema_crossover":
            set_int("fast", 1.0 + (0.15 * intensity), high=100)
            set_int("slow", slow_factor, high=250)
            set_int("trend", 1.0 + (0.1 * intensity), high=500)
            if "fast" in params and "slow" in params and int(params["fast"]) >= int(params["slow"]):
                params["slow"] = int(params["fast"]) + 1
        elif strategy_name == "rsi":
            if "oversold" in params:
                params["oversold"] = int(rounded(clamp(number("oversold", 30) - (5 * intensity), 10, 45)))
            if "overbought" in params:
                params["overbought"] = int(rounded(clamp(number("overbought", 70) + (5 * intensity), 55, 90)))
            set_int("period", 1.0 + (0.1 * intensity), high=50)
        elif strategy_name == "macd":
            set_int("fast", 1.0 + (0.1 * intensity), high=50)
            set_int("slow", 1.0 + (0.15 * intensity), high=100)
            set_int("signal", 1.0 + (0.1 * intensity), high=50)
            if "fast" in params and "slow" in params and int(params["fast"]) >= int(params["slow"]):
                params["slow"] = int(params["fast"]) + 1
        elif strategy_name == "volume_spike":
            set_int("period", slow_factor, high=120)
            set_float("multiplier", strict_factor, low=1.0, high=5.0, step=0.05)
        elif strategy_name == "supertrend":
            set_int("period", 1.0 + (0.15 * intensity), high=80)
            set_float("multiplier", strict_factor, low=1.0, high=8.0, step=0.1)
        elif strategy_name == "bollinger_bands":
            set_int("period", 1.0 + (0.1 * intensity), high=100)
            set_float("stddev", 1.0 + (0.15 * intensity), low=1.0, high=4.0, step=0.1)
        elif strategy_name == "vwap":
            set_int("period", slow_factor, high=240)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)
        elif strategy_name == "fibonacci":
            set_int("lookback", 1.0 + (0.15 * intensity), high=500)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)
        elif strategy_name == "support_resistance":
            set_int("lookback", 1.0 + (0.15 * intensity), high=500)
            set_int("pivot_window", 1.0 + (0.25 * intensity), high=20)
            set_float("tolerance_pct", tolerance_factor, low=0.0005, high=0.02, step=0.0001)

        return params

    def _result(
        self,
        applied: bool,
        eligible: bool,
        reason: str,
        overall: PerformanceStats,
        changes: list[CalibrationChange],
        strategy_adjustments: dict[str, dict[str, Any]],
        settings: dict[str, Any],
    ) -> CalibrationResult:
        return CalibrationResult(
            applied=applied,
            eligible=eligible,
            reason=reason,
            overall=overall,
            changes=changes,
            strategy_adjustments=strategy_adjustments,
            settings=settings,
        )


def serialize_calibration(result: CalibrationResult) -> dict[str, Any]:
    return {
        "applied": result.applied,
        "eligible": result.eligible,
        "reason": result.reason,
        "overall": serialize_stats(result.overall),
        "changes": [change.__dict__ for change in result.changes],
        "strategy_adjustments": result.strategy_adjustments,
        "settings": result.settings,
    }
