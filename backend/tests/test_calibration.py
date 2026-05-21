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


def _weights_settings() -> dict:
    return {
        "min_strategy_trades": 8,
        "lookback_trades": 500,
        "min_weight": 1,
        "max_weight": 30,
        "auto_disable_enabled": True,
        "auto_disable_min_trades": 25,
        "auto_disable_win_rate": 0.30,
    }


def test_auto_disable_chronic_underperformer_with_safety_floor() -> None:
    losing = StrategyConfig(
        strategy_name="rsi",
        timeframe="15m",
        enabled=True,
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=9,
    )
    keeper = StrategyConfig(
        strategy_name="macd",
        timeframe="15m",
        enabled=True,
        params={"fast": 12, "slow": 26, "signal": 9},
        weight=12,
    )
    db = FakeStrategyDb([losing, keeper])

    analyzer = FakeAnalyzer(  # type: ignore[arg-type]
        {
            "rsi": stats(win_rate=0.25, pnl=-200, profit_factor=0.6),
            "macd": stats(win_rate=0.55, pnl=120, profit_factor=1.4),
        }
    )
    changes = AutoCalibrationEngine(analyzer=analyzer)._calibrate_strategy_weights(
        db,  # type: ignore[arg-type]
        _weights_settings(),
    )

    assert losing.enabled is False
    assert keeper.enabled is True
    disable_changes = [c for c in changes if c.target == "strategy_enabled"]
    assert len(disable_changes) == 1
    assert disable_changes[0].key == "15m:rsi"
    assert "auto-disabled" in disable_changes[0].reason


def test_auto_disable_keeps_at_least_one_strategy_per_timeframe() -> None:
    only_losing = StrategyConfig(
        strategy_name="rsi",
        timeframe="15m",
        enabled=True,
        params={"period": 14, "oversold": 30, "overbought": 70},
        weight=9,
    )
    db = FakeStrategyDb([only_losing])

    analyzer = FakeAnalyzer(  # type: ignore[arg-type]
        {"rsi": stats(win_rate=0.20, pnl=-300, profit_factor=0.5)}
    )
    changes = AutoCalibrationEngine(analyzer=analyzer)._calibrate_strategy_weights(
        db,  # type: ignore[arg-type]
        _weights_settings(),
    )

    assert only_losing.enabled is True
    assert all(c.target != "strategy_enabled" for c in changes)


def _risk_settings() -> dict:
    return {
        "min_signal_score_floor": 55,
        "min_signal_score_ceiling": 80,
        "min_risk_reward_floor": 2.0,
        "min_risk_reward_ceiling": 3.0,
        "min_risk_per_trade_pct": 0.25,
        "deep_defensive_slope_threshold": -2.0,
    }


def test_deep_defensive_activates_on_strongly_negative_equity_slope() -> None:
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.45, pnl=-50, profit_factor=0.95),
        _risk_settings(),
        equity_slope_value=-5.0,
    )

    risk = db.rows["risk"].value
    state = db.rows["auto_calibration_state"].value
    assert risk["min_signal_score"] == 72  # 60 + 12
    assert risk["min_risk_reward"] == 2.4
    assert risk["max_risk_per_trade_pct"] == 0.5
    assert state["deep_defensive_active"] is True


def test_deep_defensive_requires_two_positive_runs_to_exit() -> None:
    db = FakeDb({"min_signal_score": 72, "min_risk_reward": 2.4, "max_risk_per_trade_pct": 0.5})
    db.rows["auto_calibration_state"] = AppSetting(
        key="auto_calibration_state",
        value={"deep_defensive_active": True, "deep_defensive_positive_streak": 0},
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.50, pnl=50, profit_factor=1.1),
        _risk_settings(),
        equity_slope_value=0.5,
    )

    state_after_first = db.rows["auto_calibration_state"].value
    assert state_after_first["deep_defensive_active"] is True
    assert state_after_first["deep_defensive_positive_streak"] == 1

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.55, pnl=80, profit_factor=1.3),
        _risk_settings(),
        equity_slope_value=0.5,
    )

    state_after_second = db.rows["auto_calibration_state"].value
    assert state_after_second["deep_defensive_active"] is False
    assert state_after_second["deep_defensive_positive_streak"] == 2


def test_ambiguous_slope_escapes_deep_defensive() -> None:
    """F-05: con pochi trade chiusi (slope inattendibile) il deep defensive viene
    bypassato — altrimenti il sistema resta bloccato a 0 segnali."""
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})

    # Pochi trade chiusi → slope ambiguo. Anche con equity_slope < threshold,
    # il deep defensive non si attiva.
    low_trades_stats = PerformanceStats(
        trades=3,
        wins=1,
        losses=2,
        win_rate=0.33,
        realized_pnl=-10.0,
        gross_profit=5,
        gross_loss=15,
        profit_factor=0.5,
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        low_trades_stats,
        {**_risk_settings(), "min_trades_for_slope": 10},
        equity_slope_value=-5.0,
    )

    risk = db.rows["risk"].value
    state = db.rows["auto_calibration_state"].value
    # Risk settings tornano al baseline (60 / 2.0 / 1.0), non al deep defensive (72 / 2.4 / 0.5)
    assert risk["min_signal_score"] == 60
    assert risk["max_risk_per_trade_pct"] == 1.0
    assert state["deep_defensive_active"] is False
    assert state.get("ambiguous_slope_override") is True


def test_deep_defensive_exits_via_recent_slope_before_full_slope_recovers() -> None:
    """Il deep defensive può uscire se il trend recente (ultimi N trade) è positivo
    per abbastanza cicli consecutivi, anche quando lo slope sull'intera finestra
    storica rimane negativo."""
    db = FakeDb({"min_signal_score": 72, "min_risk_reward": 2.4, "max_risk_per_trade_pct": 0.5})
    db.rows["auto_calibration_state"] = AppSetting(
        key="auto_calibration_state",
        value={
            "deep_defensive_active": True,
            "deep_defensive_positive_streak": 0,
            "deep_defensive_recent_positive_streak": 2,  # già 2 cicli positivi
        },
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.50, pnl=-30, profit_factor=0.95),
        {**_risk_settings(), "recent_slope_exit_cycles": 3},
        equity_slope_value=-0.5,   # slope globale ancora negativo
        recent_slope_value=1.2,    # ma trend recente positivo → streak diventa 3
    )

    state = db.rows["auto_calibration_state"].value
    assert state["deep_defensive_active"] is False
    assert state["deep_defensive_recent_positive_streak"] == 3


def test_defensive_mode_exits_when_recent_profit_factor_above_one() -> None:
    """Se profit_factor recente > 1.0 usciamo dal defensive anche con storico negativo.
    Copre il caso 40% WR con RR 1:3: PF=2.0, sistema profittevole nonostante WR basso."""
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})

    # 40% WR con RR 1:3 → gross_profit=120, gross_loss=60 → PF=2.0
    recent_40wr_rr3 = PerformanceStats(
        trades=30,
        wins=12,
        losses=18,
        win_rate=0.40,
        realized_pnl=60.0,
        gross_profit=120.0,
        gross_loss=60.0,
        profit_factor=2.0,
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.35, pnl=-500, profit_factor=0.8),  # storico debole
        {**_risk_settings(), "recent_window": 50},
        recent_stats=recent_40wr_rr3,
    )

    risk = db.rows["risk"].value
    # PF recente > 1.0 → baseline, nonostante WR=40% e storico negativo
    assert risk["min_signal_score"] == 60
    assert risk["min_risk_reward"] == 2.0
    assert risk["max_risk_per_trade_pct"] == 1.0


def test_defensive_mode_stays_when_recent_profit_factor_below_one() -> None:
    """Se profit_factor recente <= 1.0 il defensive rimane attivo,
    anche se il win rate fosse alto (50% WR con RR 1:1 dà PF=1.0, non > 1.0)."""
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})

    recent_losing = PerformanceStats(
        trades=30,
        wins=15,
        losses=15,
        win_rate=0.50,
        realized_pnl=-10.0,
        gross_profit=150.0,
        gross_loss=160.0,
        profit_factor=0.94,
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.35, pnl=-500, profit_factor=0.8),
        {**_risk_settings(), "recent_window": 50},
        recent_stats=recent_losing,
    )

    risk = db.rows["risk"].value
    # PF recente <= 1.0 → defensive rimane attivo
    assert risk["min_signal_score"] == 68  # 60 + 8
    assert risk["min_risk_reward"] == 2.25
    assert risk["max_risk_per_trade_pct"] == 0.75
