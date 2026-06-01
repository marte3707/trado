from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.enums import Direction
from app.db.models import AppSetting, Position, StrategyConfig
from app.engine import calibration as calibration_module
from app.engine.calibration import AutoCalibrationEngine
from app.engine.edge_profile import edge_adjustment_for_stats, signal_threshold_adjustment
from app.engine.performance import PerformanceStats, SignalPerformanceAnalyzer


def _positions_matching(target: PerformanceStats) -> list[Position]:
    """Crea posizioni mock che, passate a summarize_positions, restituiscono
    gli stats target. Usato dai test per simulare lo storico di una strategia
    senza dover persistere oggetti reali in DB."""
    win_pnl = target.gross_profit / target.wins if target.wins else 0.0
    loss_pnl = -target.gross_loss / target.losses if target.losses else 0.0
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    positions: list[Position] = []
    for i in range(target.wins):
        positions.append(
            Position(realized_pnl=Decimal(str(win_pnl)), closed_at=base + timedelta(hours=i))
        )
    for i in range(target.losses):
        positions.append(
            Position(
                realized_pnl=Decimal(str(loss_pnl)),
                closed_at=base + timedelta(hours=target.wins + i),
            )
        )
    # Ritorna desc come `closed_paper_positions`
    positions.sort(key=lambda p: p.closed_at, reverse=True)
    return positions


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
    # Con Wilson UB su 30 trade: serve WR puntuale ~0.23 perché UB < 0.40 + PnL < 0
    # → factor 0.6. Le vecchie soglie WR-pure davano falsi positivi su campioni piccoli.
    factor, reason = AutoCalibrationEngine()._strategy_factor(
        stats(win_rate=0.23, pnl=-100, profit_factor=0.5)
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


def test_edge_penalty_flags_weak_long_but_not_healthy_short() -> None:
    long_penalty = edge_adjustment_for_stats(
        PerformanceStats(
            trades=32,
            wins=7,
            losses=25,
            win_rate=7 / 32,
            realized_pnl=-420.0,
            gross_profit=160.0,
            gross_loss=580.0,
            profit_factor=0.28,
        ),
        weak_trades=20,
        weak_pf=0.8,
        weak_wr=0.35,
        weak_adjustment=8,
        severe_trades=30,
        severe_pf=0.6,
        severe_wr=0.25,
        severe_adjustment=12,
    )
    short_penalty = edge_adjustment_for_stats(
        PerformanceStats(
            trades=35,
            wins=18,
            losses=17,
            win_rate=18 / 35,
            realized_pnl=220.0,
            gross_profit=620.0,
            gross_loss=400.0,
            profit_factor=1.55,
        ),
        weak_trades=20,
        weak_pf=0.8,
        weak_wr=0.35,
        weak_adjustment=8,
        severe_trades=30,
        severe_pf=0.6,
        severe_wr=0.25,
        severe_adjustment=12,
    )

    assert long_penalty == 12
    assert short_penalty == 0


def test_signal_threshold_adjustment_combines_and_caps_penalties() -> None:
    risk = {
        "directional_score_adjustments": {"LONG:5m": {"adjustment": 12}},
        "score_bucket_adjustments": {"LONG:60-69": {"adjustment": 8}},
        "market_score_adjustments": {"42": {"adjustment": 10}},
        "timeframe_score_adjustments": {"5m": {"adjustment": 5}},
        "max_combined_score_adjustment": 15,
    }

    adjustment = signal_threshold_adjustment(
        risk,
        market_id=42,
        timeframe="5m",
        direction=Direction.LONG,
        score=64,
    )

    assert adjustment["raw_total"] == 35
    assert adjustment["total"] == 15
    assert adjustment["components"]["directional"] == 12
    assert adjustment["score_bucket"] == "60-69"


def test_edge_calibration_clears_recovered_penalties(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDb(
        {
            "directional_score_adjustments": {"LONG:5m": {"adjustment": 12}},
            "score_bucket_adjustments": {"LONG:60-69": {"adjustment": 8}},
            "market_score_adjustments": {"42": {"adjustment": 10}},
            "timeframe_score_adjustments": {"5m": {"adjustment": 5}},
            "max_combined_score_adjustment": 15,
            "entry_quality_filter_enabled": True,
        }
    )
    monkeypatch.setattr(
        calibration_module,
        "compute_edge_adjustments",
        lambda *args, **kwargs: {
            "directional_score_adjustments": {},
            "score_bucket_adjustments": {},
            "market_score_adjustments": {},
            "timeframe_score_adjustments": {},
        },
    )

    changes = AutoCalibrationEngine()._calibrate_edge_adjustments(
        db,  # type: ignore[arg-type]
        {"lookback_trades": 500},
    )

    risk = db.rows["risk"].value
    assert risk["directional_score_adjustments"] == {}
    assert risk["score_bucket_adjustments"] == {}
    assert any(change.key == "directional_score_adjustments" for change in changes)


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
        self._real_analyzer = SignalPerformanceAnalyzer()

    def strategy_stats(self, db, timeframe=None, market_id=None, limit=500):  # noqa: ANN001
        return self.by_strategy

    def strategy_positions(self, db, timeframe=None, market_id=None, limit=500):  # noqa: ANN001
        return {name: _positions_matching(stats) for name, stats in self.by_strategy.items()}

    def summarize_positions(self, positions):  # noqa: ANN001
        return self._real_analyzer.summarize_positions(positions)


def test_calibrate_strategy_params_updates_strategy_config_params() -> None:
    strategy = StrategyConfig(
        strategy_name="volume_spike",
        timeframe="15m",
        enabled=True,
        params={"period": 20, "multiplier": 1.5},
        weight=7,
    )
    db = FakeStrategyDb([strategy])

    # Stats coerenti: WR=0.4 + PF=0.9 + pnl negativo (era inconsistente nel vecchio
    # test che non simulava posizioni reali; ora summarize_positions ricalcola PF
    # dai dati). PF<1.0 → defensive intensity=0.5 per volume_spike.
    target = PerformanceStats(
        trades=30, wins=12, losses=18, win_rate=0.4,
        realized_pnl=-50.0, gross_profit=450.0, gross_loss=500.0, profit_factor=0.9,
    )
    changes = AutoCalibrationEngine(
        analyzer=FakeAnalyzer({"volume_spike": target})  # type: ignore[arg-type]
    )._calibrate_strategy_params(
        db,  # type: ignore[arg-type]
        # walk_forward_enabled=False: questo test isola la pure decisione su full stats
        # senza il filtro holdout (testato separatamente)
        {
            "min_strategy_trades": 8,
            "lookback_trades": 500,
            "walk_forward_enabled": False,
        },
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

    # Con Wilson UB strict: WR puntuale 0.25 su 30 trade dà UB≈0.42 (sopra 0.30) →
    # NON viene disabled. Per triggerare la disable serve UB < soglia. Con WR=0.05
    # su 30 trade (1 win): UB≈0.20 < 0.30 → disable.
    analyzer = FakeAnalyzer(  # type: ignore[arg-type]
        {
            "rsi": stats(win_rate=0.05, pnl=-200, profit_factor=0.1),
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


def test_deep_defensive_cooldown_blocks_immediate_reentry() -> None:
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})
    db.rows["auto_calibration_state"] = AppSetting(
        key="auto_calibration_state",
        value={
            "deep_defensive_active": False,
            "deep_defensive_cooldown_remaining": 6,
            "deep_defensive_cooldown_trade_count": 30,
        },
    )

    changes = AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.30, pnl=-200, profit_factor=0.5),
        {
            **_risk_settings(),
            "deep_defensive_cooldown_runs": 6,
            "deep_defensive_cooldown_min_new_trades": 3,
        },
        equity_slope_value=-5.0,
    )

    risk = db.rows["risk"].value
    state = db.rows["auto_calibration_state"].value
    assert risk["min_signal_score"] == 60
    assert risk["min_risk_reward"] == 2.0
    assert risk["max_risk_per_trade_pct"] == 1.0
    assert state["deep_defensive_active"] is False
    assert state["deep_defensive_cooldown_remaining"] == 5
    assert any(c.target == "calibration_state" and c.key == "deep_defensive_cooldown" for c in changes)


def test_deep_defensive_cooldown_expires_after_new_trades() -> None:
    db = FakeDb({"min_signal_score": 60, "min_risk_reward": 2.0, "max_risk_per_trade_pct": 1.0})
    db.rows["auto_calibration_state"] = AppSetting(
        key="auto_calibration_state",
        value={
            "deep_defensive_active": False,
            "deep_defensive_cooldown_remaining": 6,
            "deep_defensive_cooldown_trade_count": 27,
        },
    )

    AutoCalibrationEngine()._calibrate_risk_settings(
        db,  # type: ignore[arg-type]
        stats(win_rate=0.30, pnl=-200, profit_factor=0.5),
        {
            **_risk_settings(),
            "deep_defensive_cooldown_runs": 6,
            "deep_defensive_cooldown_min_new_trades": 3,
        },
        equity_slope_value=-5.0,
    )

    risk = db.rows["risk"].value
    state = db.rows["auto_calibration_state"].value
    assert state["deep_defensive_cooldown_remaining"] == 0
    assert state["deep_defensive_active"] is True
    assert risk["min_signal_score"] == 72


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


# ---------------------------------------------------------------------------
# Walk-forward holdout validation (§3.1)
# ---------------------------------------------------------------------------


def _make_positions_sequence(pnls: list[float]) -> list[Position]:
    """Crea positions in ordine temporale (pnls[0] = più vecchia), ritorna desc."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    positions = [
        Position(realized_pnl=Decimal(str(pnl)), closed_at=base + timedelta(hours=i))
        for i, pnl in enumerate(pnls)
    ]
    positions.sort(key=lambda p: p.closed_at, reverse=True)
    return positions


def test_holdout_split_stats_separates_train_and_test_temporally() -> None:
    # 10 trade chronologici: prima metà tutta loss, seconda metà tutta win
    positions = _make_positions_sequence([-5.0] * 5 + [10.0] * 5)
    split = SignalPerformanceAnalyzer.holdout_split_stats(
        positions, train_ratio=0.5, min_samples_per_side=3
    )

    assert split is not None
    train, test = split
    # Train = prima metà (loss), Test = seconda metà (win)
    assert train.win_rate == 0.0
    assert test.win_rate == 1.0


def test_holdout_split_stats_returns_none_when_insufficient_samples() -> None:
    positions = _make_positions_sequence([1.0, 2.0, 3.0])  # solo 3 trade

    assert SignalPerformanceAnalyzer.holdout_split_stats(positions, min_samples_per_side=5) is None


def test_holdout_blocks_change_when_train_and_test_disagree() -> None:
    """Se train dice 'underperform' (factor < 1) e test dice 'overperform' (factor > 1)
    → factor 1.0 (freeze): la decisione non regge fuori campione."""
    # 30 trade tot, train_ratio=0.7 → primi 21 nel train, ultimi 9 nel test
    # Train: 18 loss + 3 win → factor 0.6 (PF=0.33, pnl<0, wr_ub<0.40)
    # Test: 9 win consecutivi → factor 1.2 (wr_lb>0.55, pnl>0, PF=None>1.25)
    positions = _make_positions_sequence([-5.0] * 18 + [10.0] * 3 + [10.0] * 9)
    factor, reason = AutoCalibrationEngine()._strategy_factor_holdout_validated(
        positions,
        enabled=True,
        train_ratio=0.7,
        min_per_side=5,
    )

    assert factor == 1.0
    assert "holdout instability" in reason


def test_holdout_passes_when_train_and_test_agree() -> None:
    """Se train e test concordano (entrambi sotto baseline) → applica factor full."""
    # Trade tutti perdenti → factor < 1.0 ovunque (concordant)
    positions = _make_positions_sequence([-10.0] * 30)
    factor, reason = AutoCalibrationEngine()._strategy_factor_holdout_validated(
        positions,
        enabled=True,
        train_ratio=0.7,
        min_per_side=5,
    )

    assert factor < 1.0
    assert "holdout ok" in reason


def test_holdout_disabled_returns_full_factor_directly() -> None:
    positions = _make_positions_sequence([-10.0] * 30)
    factor_enabled, _ = AutoCalibrationEngine()._strategy_factor_holdout_validated(
        positions, enabled=True, train_ratio=0.7, min_per_side=5
    )
    factor_disabled, reason = AutoCalibrationEngine()._strategy_factor_holdout_validated(
        positions, enabled=False, train_ratio=0.7, min_per_side=5
    )

    # Senza holdout, è un puro pass-through di _strategy_factor
    assert factor_disabled == factor_enabled  # in questo caso concordant → uguali
    assert "holdout" not in reason


def test_time_weighted_summarize_weights_recent_higher() -> None:
    # 10 vecchie loss + 10 recenti win → win_rate "ingenuo" = 0.5
    # Con time decay, le recenti pesano molto di più → win_rate pesato > 0.5
    positions = _make_positions_sequence([-1.0] * 10 + [1.0] * 10)
    weighted = SignalPerformanceAnalyzer.time_weighted_summarize(positions, half_life=5)
    plain = SignalPerformanceAnalyzer().summarize_positions(positions)

    assert plain.win_rate == 0.5
    assert weighted.win_rate > 0.5


def test_factor_stability_freezes_change_when_history_oscillates() -> None:
    """Factor con varianza elevata negli ultimi run → freeze del peso (factor=1.0)."""
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
    # Seed factor_history con oscillazione (std alto) per rsi
    db.rows["auto_calibration_state"] = AppSetting(
        key="auto_calibration_state",
        value={"factor_history": {"15m:rsi": [0.6, 1.2, 0.6, 1.2]}},
    )
    # Stats che da soli triggerebbero factor 0.6 (pnl<0, wr_ub<0.40, PF<0.8)
    bad_stats = PerformanceStats(
        trades=30, wins=2, losses=28, win_rate=0.067,
        realized_pnl=-200.0, gross_profit=10.0, gross_loss=210.0, profit_factor=0.05,
    )
    good_stats = stats(win_rate=0.55, pnl=120, profit_factor=1.4)

    analyzer = FakeAnalyzer({"rsi": bad_stats, "macd": good_stats})  # type: ignore[arg-type]
    changes = AutoCalibrationEngine(analyzer=analyzer)._calibrate_strategy_weights(
        db,  # type: ignore[arg-type]
        {
            **_weights_settings(),
            "walk_forward_enabled": False,  # isolare il test sulla stability
            "factor_history_window": 5,
            "factor_history_std_threshold": 0.25,
        },
    )

    weight_changes = [c for c in changes if c.target == "strategy_weight" and c.key == "15m:rsi"]
    if weight_changes:
        assert "stability freeze" in weight_changes[0].reason
    else:
        # Se non c'è weight change, vuol dire che il factor è rimasto a 1.0 → niente change.
        # Anche questo è una freeze valida.
        assert losing.weight == 9
