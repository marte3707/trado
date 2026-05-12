from app.db.models import AppSetting, StrategyConfig
from app.engine.calibration import AutoCalibrationEngine
from app.engine.performance import PerformanceStats


class FakeDb:
    def __init__(self, settings: dict) -> None:
        self.rows = {
            "auto_calibration_baseline": AppSetting(
                key="auto_calibration_baseline",
                value={
                    "risk": {
                        "min_signal_score": 60,
                        "min_risk_reward": 2.0,
                        "max_risk_per_trade_pct": 1.0,
                    },
                    "strategy_weights": {},
                    "strategy_params": {},
                },
            ),
            "risk": AppSetting(key="risk", value=settings),
        }

    def get(self, model, key: str):  # noqa: ANN001
        return self.rows.get(key)

    def add(self, row: AppSetting) -> None:
        self.rows[row.key] = row


def stats(win_rate: float, pnl: float, profit_factor: float | None) -> PerformanceStats:
    return PerformanceStats(
        trades=30,
        wins=round(30 * win_rate),
        losses=30 - round(30 * win_rate),
        win_rate=win_rate,
        realized_pnl=pnl,
        gross_profit=max(pnl, 0),
        gross_loss=abs(min(pnl, 0)),
        profit_factor=profit_factor,
    )


def test_weak_historical_performance_tightens_risk_settings() -> None:
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})

    changes = AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.32, pnl=-150, profit_factor=0.7),
        {
            "min_signal_score_floor": 55,
            "min_signal_score_ceiling": 80,
            "min_risk_reward_floor": 2.0,
            "min_risk_reward_ceiling": 3.0,
            "min_risk_per_trade_pct": 0.25,
        },
    )

    risk = db.rows["risk"].value
    assert risk["min_signal_score"] == 68
    assert risk["min_risk_reward"] == 2.25
    assert risk["max_risk_per_trade_pct"] == 0.75
    assert {change.key for change in changes} == {
        "min_signal_score",
        "min_risk_reward",
        "max_risk_per_trade_pct",
    }


def test_strong_historical_performance_restores_baseline_not_extra_risk() -> None:
    db = FakeDb({"min_signal_score": 72, "min_risk_reward": 2.4, "max_risk_per_trade_pct": 0.5})

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.65, pnl=200, profit_factor=1.5),
        {
            "min_signal_score_floor": 55,
            "min_signal_score_ceiling": 80,
            "min_risk_reward_floor": 2.0,
            "min_risk_reward_ceiling": 3.0,
            "min_risk_per_trade_pct": 0.25,
        },
    )

    risk = db.rows["risk"].value
    assert risk["min_signal_score"] == 60
    assert risk["min_risk_reward"] == 2.0
    assert risk["max_risk_per_trade_pct"] == 1.0


def test_strategy_factor_penalizes_unprofitable_strategy() -> None:
    factor, reason = AutoCalibrationEngine()._strategy_factor(
        stats(win_rate=0.5, pnl=-25, profit_factor=0.9)
    )

    assert factor == 0.6
    assert "underperformed" in reason


def test_strategy_params_become_more_selective_after_weak_performance() -> None:
    params, reason = AutoCalibrationEngine()._strategy_params_for_stats(
        "rsi",
        {"period": 14, "oversold": 30, "overbought": 70},
        stats(win_rate=0.3, pnl=-100, profit_factor=0.7),
    )

    assert params == {"period": 15, "oversold": 25, "overbought": 75}
    assert "more selective" in reason


class FakeQuery:
    def __init__(self, rows: list[StrategyConfig]) -> None:
        self.rows = rows

    def all(self) -> list[StrategyConfig]:
        return self.rows


class FakeStrategyDb(FakeDb):
    def __init__(self, strategies: list[StrategyConfig]) -> None:
        super().__init__({})
        self.strategies = strategies
        self.rows["auto_calibration_baseline"].value["strategy_params"] = {
            f"{strategy.timeframe}:{strategy.strategy_name}": dict(strategy.params or {})
            for strategy in strategies
        }

    def query(self, model):  # noqa: ANN001
        assert model is StrategyConfig
        return FakeQuery(self.strategies)


class FakeAnalyzer:
    def __init__(self, by_strategy: dict[str, PerformanceStats]) -> None:
        self.by_strategy = by_strategy

    def strategy_stats(self, db, timeframe=None, market_id=None, limit=500):  # noqa: ANN001
        return self.by_strategy


def test_calibrate_strategy_params_updates_strategy_config_params() -> None:
    strategy = StrategyConfig(
        strategy_name="volume_spike",
        timeframe="15m",
        enabled=True,
        params={"period": 20, "multiplier": 1.5},
        weight=7,
    )
    db = FakeStrategyDb([strategy])

    changes = AutoCalibrationEngine(
        analyzer=FakeAnalyzer({"volume_spike": stats(win_rate=0.4, pnl=10, profit_factor=0.9)})  # type: ignore[arg-type]
    )._calibrate_strategy_params(
        db,  # type: ignore[arg-type]
        {"min_strategy_trades": 8, "lookback_trades": 500},
    )

    assert strategy.params == {"period": 22, "multiplier": 1.7}
    assert len(changes) == 1
    assert changes[0].target == "strategy_params"
