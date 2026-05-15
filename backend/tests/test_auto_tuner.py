from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.models import AppSetting, DecisionLog, Position
from app.engine.auto_tuner import AutoTuner


class FakeAnalyzer:
    def __init__(self, positions: list[Position]) -> None:
        self.positions = positions

    def closed_paper_positions(
        self,
        db,
        timeframe: str | None = None,
        market_id: int | None = None,
        limit: int = 500,
    ) -> list[Position]:
        return list(self.positions[:limit])

    def summarize_positions(self, positions: list[Position]):
        from app.engine.performance import PerformanceStats

        trades = len(positions)
        wins = sum(1 for p in positions if (p.realized_pnl or 0) > 0)
        losses = trades - wins
        pnl = sum(float(p.realized_pnl or 0) for p in positions)
        gross_profit = sum(float(p.realized_pnl or 0) for p in positions if (p.realized_pnl or 0) > 0)
        gross_loss = abs(sum(float(p.realized_pnl or 0) for p in positions if (p.realized_pnl or 0) <= 0))
        pf = (gross_profit / gross_loss) if gross_loss else None
        return PerformanceStats(
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=(wins / trades) if trades else 0.0,
            realized_pnl=pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=pf,
        )


class FakeDb:
    def __init__(self) -> None:
        self.app_settings: dict[str, AppSetting] = {}
        self.decision_logs: list[DecisionLog] = []
        self.commit_count = 0

    def get(self, model, key):  # noqa: ANN001
        if model is AppSetting:
            return self.app_settings.get(key)
        return None

    def add(self, row) -> None:
        if isinstance(row, AppSetting):
            self.app_settings[row.key] = row
        elif isinstance(row, DecisionLog):
            self.decision_logs.append(row)

    def commit(self) -> None:
        self.commit_count += 1


def _position(*, pnl: float, closed_at: datetime) -> Position:
    position = Position(
        market_id=1,
        mode="paper",
        side="LONG",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="closed",
    )
    position.realized_pnl = Decimal(str(pnl))
    position.closed_at = closed_at
    position.metadata_json = {}
    return position


def _positions_with_slope(count: int, pnl_per_trade: float, start: datetime) -> list[Position]:
    return [
        _position(pnl=pnl_per_trade, closed_at=start + timedelta(minutes=i))
        for i in range(count)
    ]


def _config(**overrides) -> AppSetting:
    sequence = [
        {
            "key": "regime_filter",
            "label": "Regime",
            "patch": {"regime_filter_enabled": True},
            "revert": {"regime_filter_enabled": False},
        },
        {
            "key": "mtf_confluence",
            "label": "MTF",
            "patch": {"mtf_confluence_enabled": True},
            "revert": {"mtf_confluence_enabled": False},
        },
    ]
    value = {
        "enabled": True,
        "min_trades_per_step": 5,
        "improvement_threshold": 0.0,
        "sequence": sequence,
    }
    value.update(overrides)
    return AppSetting(key="auto_tuner_config", value=value)


def test_run_disabled_returns_disabled() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config(enabled=False)
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.run(db)

    assert result == {"status": "disabled"}


def test_run_starts_first_step_when_idle() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config()
    db.app_settings["risk"] = AppSetting(key="risk", value={"min_signal_score": 70})
    positions = _positions_with_slope(20, -1.0, datetime(2026, 5, 14, tzinfo=timezone.utc))
    # ordine: closed_paper_positions ritorna ordinato desc per closed_at — qui inverto per simulare
    positions.reverse()
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    result = tuner.run(db)

    assert result["status"] == "started"
    assert result["step"] == "regime_filter"
    state = db.app_settings["auto_tuner_state"].value
    assert state["current_step_key"] == "regime_filter"
    assert state["trades_at_activation"] == 20
    risk_value = db.app_settings["risk"].value
    assert risk_value["regime_filter_enabled"] is True


def test_run_in_progress_when_not_enough_trades() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config()
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 20,
            "metrics_before": {"equity_slope": -10.0},
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": True, "min_signal_score": 70}
    )
    # Solo 2 nuovi trade dopo l'attivazione → in_progress
    positions = list(reversed(_positions_with_slope(22, -1.0, activated - timedelta(days=1))))
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    result = tuner.run(db)

    assert result["status"] == "in_progress"
    assert result["trades_done"] >= 0


def test_run_advances_when_slope_improves() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config(min_trades_per_step=10)
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "metrics_before": {"equity_slope": -50.0, "win_rate": 0.1, "trades": 5},
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": True, "min_signal_score": 70}
    )
    # 12 trade post-attivazione con pnl positivo → slope > -50
    positions = list(
        reversed(
            _positions_with_slope(12, 5.0, activated + timedelta(minutes=1))
        )
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    result = tuner.run(db)

    assert result["status"] == "advanced"
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert len(history) == 1
    assert history[0]["status"] == "success"
    # Lo step regime_filter rimane applicato
    risk_value = db.app_settings["risk"].value
    assert risk_value["regime_filter_enabled"] is True


def test_run_reverts_when_slope_worsens() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config(min_trades_per_step=10)
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "metrics_before": {"equity_slope": -5.0},
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": True, "min_signal_score": 70}
    )
    # 12 trade post-attivazione con pnl negativo costante → slope molto < -5
    positions = list(
        reversed(
            _positions_with_slope(12, -10.0, activated + timedelta(minutes=1))
        )
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    result = tuner.run(db)

    assert result["status"] == "reverted"
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert history[0]["status"] == "reverted"
    # Rollback: regime_filter_enabled deve essere False
    risk_value = db.app_settings["risk"].value
    assert risk_value["regime_filter_enabled"] is False


def test_run_completes_after_all_steps() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": None,
            "step_history": [
                {"key": "regime_filter", "status": "success"},
                {"key": "mtf_confluence", "status": "reverted"},
            ],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.run(db)

    assert result["status"] == "completed"


def test_pause_and_resume_toggle_enabled_flag() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config()
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    tuner.pause(db)
    assert db.app_settings["auto_tuner_config"].value["enabled"] is False

    tuner.resume(db)
    assert db.app_settings["auto_tuner_config"].value["enabled"] is True


def test_rollback_current_reverts_active_step() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "activated_at": "2026-05-14T12:00:00+00:00",
            "trades_at_activation": 10,
            "metrics_before": {"equity_slope": -5.0},
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": True}
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.rollback_current(db)

    assert result["ok"] is True
    assert db.app_settings["risk"].value["regime_filter_enabled"] is False
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert history[0]["status"] == "reverted"
    assert history[0]["manual"] is True


def test_status_returns_progress_when_step_active() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config(min_trades_per_step=10)
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 10,
            "metrics_before": {"equity_slope": -5.0},
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(key="risk", value={})
    positions = _positions_with_slope(13, -1.0, activated)
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    status = tuner.status(db)

    assert status["enabled"] is True
    assert status["current_step_key"] == "regime_filter"
    assert status["progress"]["trades_done"] == 3
    assert status["progress"]["trades_required"] == 10
