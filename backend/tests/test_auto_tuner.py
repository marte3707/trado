import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.models import AppSetting, DecisionLog, Position
from app.engine.auto_tuner import AutoTuner, active_ab_overlay


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


def _position(
    *,
    pnl: float,
    closed_at: datetime,
    arm: str | None = None,
    step_key: str = "regime_filter",
) -> Position:
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
    meta: dict = {}
    if arm is not None:
        meta["_auto_tuner"] = {"step_key": step_key, "arm": arm}
    position.metadata_json = meta
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


def test_positions_after_returns_empty_when_state_corrupt() -> None:
    """F-10: senza `activated_at` ritorniamo lista vuota, non l'intera collezione.
    Altrimenti `metrics_after` includerebbe le posizioni pre-attivazione."""
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    positions = _positions_with_slope(5, 1.0, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert tuner._positions_after(positions, None) == []
    assert tuner._positions_after(positions, "") == []
    # Stringa malformata → lista vuota (non solleva)
    assert tuner._positions_after(positions, "not-a-date") == []


# --- §3.10 AB testing -------------------------------------------------------


def _ab_config(**overrides) -> AppSetting:
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
        "mode": "ab_test",
        "ab_test_min_trades_per_arm": 5,
        "ab_test_significance_level": 0.05,
        "ab_test_allocation": 0.5,
        "sequence": sequence,
    }
    value.update(overrides)
    return AppSetting(key="auto_tuner_config", value=value)


def _tagged_positions(
    count: int, pnl: float, start: datetime, arm: str, step_key: str = "regime_filter"
) -> list[Position]:
    return [
        _position(
            pnl=pnl,
            closed_at=start + timedelta(minutes=i),
            arm=arm,
            step_key=step_key,
        )
        for i in range(count)
    ]


def test_ab_mode_start_does_not_modify_risk_setting() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": False, "min_signal_score": 70}
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.run(db)

    assert result["status"] == "started"
    assert result["mode"] == "ab_test"
    assert result["step"] == "regime_filter"
    state = db.app_settings["auto_tuner_state"].value
    assert state["mode"] == "ab_test"
    assert state["ab_test"]["control_overlay"] == {"regime_filter_enabled": False}
    assert state["ab_test"]["test_overlay"] == {"regime_filter_enabled": True}
    # IMPORTANTE: in modalità AB il `risk` AppSetting *non* deve essere mutato.
    # Control e test sono iniettati per-segnale dal signal_engine.
    assert db.app_settings["risk"].value["regime_filter_enabled"] is False


def test_ab_mode_in_progress_until_min_trades_per_arm() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(ab_test_min_trades_per_arm=10)
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(key="risk", value={})
    # 3 control + 3 test → entrambi sotto il min di 10
    positions = (
        _tagged_positions(3, 1.0, activated + timedelta(minutes=1), "control")
        + _tagged_positions(3, 2.0, activated + timedelta(minutes=4), "test")
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    result = tuner.run(db)

    assert result["status"] == "in_progress"
    assert result["mode"] == "ab_test"
    assert result["trades_control"] == 3
    assert result["trades_test"] == 3
    assert result["trades_required_per_arm"] == 10


def test_ab_mode_promotes_when_test_significantly_better() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(
        ab_test_min_trades_per_arm=10, ab_test_significance_level=0.05
    )
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": False}
    )
    # Test arm chiaramente migliore: mean ≈ +5 vs control mean ≈ -2.
    # Con 20 trade per arm e bassa varianza, p-value << 0.05.
    rng = random.Random(42)
    control_positions = [
        _position(
            pnl=-2.0 + rng.uniform(-0.5, 0.5),
            closed_at=activated + timedelta(minutes=i + 1),
            arm="control",
        )
        for i in range(20)
    ]
    test_positions = [
        _position(
            pnl=5.0 + rng.uniform(-0.5, 0.5),
            closed_at=activated + timedelta(minutes=i + 50),
            arm="test",
        )
        for i in range(20)
    ]
    tuner = AutoTuner(analyzer=FakeAnalyzer(control_positions + test_positions))

    result = tuner.run(db)

    assert result["status"] == "promoted"
    assert result["mode"] == "ab_test"
    assert result["mean_test"] > result["mean_control"]
    assert result["p_one_tailed"] < 0.05
    # Promozione: il patch deve essere stato applicato a `risk`.
    assert db.app_settings["risk"].value["regime_filter_enabled"] is True
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert history[0]["status"] == "promoted"
    assert history[0]["ab_test"]["n_control"] == 20
    assert history[0]["ab_test"]["n_test"] == 20


def test_ab_mode_rejects_when_not_significant() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(
        ab_test_min_trades_per_arm=10, ab_test_significance_level=0.05
    )
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
            "step_history": [],
        },
    )
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": False}
    )
    # Test arm peggio del control → improvement < 0 → rejected (nessun apply).
    rng = random.Random(7)
    control_positions = [
        _position(
            pnl=3.0 + rng.uniform(-0.3, 0.3),
            closed_at=activated + timedelta(minutes=i + 1),
            arm="control",
        )
        for i in range(15)
    ]
    test_positions = [
        _position(
            pnl=-1.0 + rng.uniform(-0.3, 0.3),
            closed_at=activated + timedelta(minutes=i + 50),
            arm="test",
        )
        for i in range(15)
    ]
    tuner = AutoTuner(analyzer=FakeAnalyzer(control_positions + test_positions))

    result = tuner.run(db)

    assert result["status"] == "rejected"
    # Rejected: `risk` non deve essere stato modificato.
    assert db.app_settings["risk"].value["regime_filter_enabled"] is False
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert history[0]["status"] == "rejected"


def test_ab_mode_ignores_untagged_positions_in_partition() -> None:
    """Posizioni senza `_auto_tuner` metadata (es. trade pre-attivazione o di
    altri step) non devono essere conteggiate in nessun arm."""
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    positions = [
        # Senza tag → escluso
        _position(pnl=1.0, closed_at=activated + timedelta(minutes=1), arm=None),
        # Step diverso → escluso
        _position(
            pnl=2.0,
            closed_at=activated + timedelta(minutes=2),
            arm="control",
            step_key="other_step",
        ),
        # Step corretto, arm control → contato
        _position(
            pnl=3.0,
            closed_at=activated + timedelta(minutes=3),
            arm="control",
        ),
    ]

    arms = tuner._partition_by_arm(positions, activated.isoformat(), "regime_filter")

    assert len(arms["control"]) == 1
    assert arms["control"][0].realized_pnl == Decimal("3.0")
    assert len(arms["test"]) == 0


def test_active_ab_overlay_returns_none_when_not_in_ab_mode() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _config(enabled=True)  # mode="sequential"
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
        },
    )
    assert active_ab_overlay(db) is None


def test_active_ab_overlay_returns_arm_when_ab_active() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 1.0,  # 100% test → deterministico
            },
        },
    )

    overlay = active_ab_overlay(db)

    assert overlay is not None
    assert overlay["step_key"] == "regime_filter"
    assert overlay["arm"] == "test"
    assert overlay["overlay"] == {"regime_filter_enabled": True}


def test_active_ab_overlay_is_deterministic_with_signal_context() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(ab_test_assignment_salt="test-salt")
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
        },
    )
    candle_time = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)

    first = active_ab_overlay(
        db,
        market_id=1,
        timeframe="5m",
        source_candle_open_time=candle_time,
    )
    second = active_ab_overlay(
        db,
        market_id=1,
        timeframe="5m",
        source_candle_open_time=candle_time,
    )

    assert first is not None
    assert first == second
    assert first["assignment_method"] == "deterministic_hash"


def test_active_ab_overlay_hash_assignment_is_roughly_balanced() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(ab_test_assignment_salt="balance-test")
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
        },
    )
    base = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)

    arms = [
        active_ab_overlay(
            db,
            market_id=i,
            timeframe="5m",
            source_candle_open_time=base + timedelta(minutes=i),
        )["arm"]  # type: ignore[index]
        for i in range(200)
    ]

    test_ratio = arms.count("test") / len(arms)
    assert 0.40 <= test_ratio <= 0.60


def test_status_exposes_ab_assignment_method() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
                "assignment_method": "deterministic_hash",
            },
            "step_history": [],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    status = tuner.status(db)

    assert status["ab_progress"]["assignment_method"] == "deterministic_hash"
    assert status["config"]["ab_test_assignment_salt"] == "trado-v1"


def test_status_exposes_ab_progress_metrics() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(ab_test_min_trades_per_arm=10)
    activated = datetime(2026, 5, 14, 12, tzinfo=timezone.utc)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
                "assignment_method": "deterministic_hash",
            },
            "step_history": [],
        },
    )
    positions = (
        _tagged_positions(4, -1.0, activated + timedelta(minutes=1), "control")
        + _tagged_positions(6, 2.0, activated + timedelta(minutes=20), "test")
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    status = tuner.status(db)

    progress = status["ab_progress"]
    assert progress["trades_control"] == 4
    assert progress["trades_test"] == 6
    assert progress["mean_delta"] == 3.0
    assert round(progress["sample_balance"], 3) == 0.667
    assert progress["control_overlay"] == {"regime_filter_enabled": False}
    assert progress["test_overlay"] == {"regime_filter_enabled": True}


def test_start_source_test_creates_guided_ab_sequence_without_mutating_risk() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(enabled=False)
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"disabled_signal_sources": ["whale_flow", "fear_greed"]}
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.start_source_test(db, "whale_flow")

    assert result["ok"] is True
    assert result["mode"] == "ab_test"
    assert result["step"] == "whale_flow_source"
    config = db.app_settings["auto_tuner_config"].value
    assert config["enabled"] is True
    assert config["mode"] == "ab_test"
    assert config["sequence"][0]["key"] == "whale_flow_source"
    assert db.app_settings["risk"].value["disabled_signal_sources"] == [
        "whale_flow",
        "fear_greed",
    ]
    state = db.app_settings["auto_tuner_state"].value
    assert state["ab_test"]["control_overlay"] == {
        "disabled_signal_sources": ["whale_flow", "fear_greed"]
    }
    assert state["ab_test"]["test_overlay"] == {"disabled_signal_sources": ["fear_greed"]}


def test_start_source_test_returns_conflict_when_step_active() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "volatility_filter",
            "mode": "ab_test",
            "step_history": [],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.start_source_test(db, "fear_greed")

    assert result["ok"] is False
    assert result["status"] == "conflict"
    assert result["active_step_key"] == "volatility_filter"


def test_active_ab_overlay_returns_control_when_allocation_zero() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.0,  # 0% test → control deterministico
            },
        },
    )

    overlay = active_ab_overlay(db)

    assert overlay is not None
    assert overlay["arm"] == "control"
    assert overlay["overlay"] == {"regime_filter_enabled": False}


def test_ab_mode_rollback_does_not_revert_risk() -> None:
    """In AB mode il patch non era stato applicato; rollback chiude solo lo step."""
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "regime_filter",
            "mode": "ab_test",
            "activated_at": "2026-05-14T12:00:00+00:00",
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"regime_filter_enabled": False},
                "test_overlay": {"regime_filter_enabled": True},
                "allocation": 0.5,
            },
            "step_history": [],
        },
    )
    # Stato "vero" su risk: regime_filter già attivato da altro flow.
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": True}
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.rollback_current(db)

    assert result["ok"] is True
    # AB rollback NON deve aver fatto revert su risk (no-op sul setting).
    assert db.app_settings["risk"].value["regime_filter_enabled"] is True
    state = db.app_settings["auto_tuner_state"].value
    assert state["ab_test"] is None
    assert state["step_history"][0]["status"] == "reverted"
    assert state["step_history"][0]["mode"] == "ab_test"


def test_welch_t_test_returns_p_value_near_one_for_equal_means() -> None:
    """Sanity check: due popolazioni con stessa media → p_one_tailed ≈ 0.5."""
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    rng = random.Random(0)
    control = [rng.gauss(0, 1) for _ in range(100)]
    test = [rng.gauss(0, 1) for _ in range(100)]
    stats = tuner._welch_t_test(control, test)
    # Per popolazioni equivalenti il p-value one-tailed si distribuisce uniforme
    # tra 0 e 1; con seed=0 verifichiamo che sia "non-significativo" (> 0.05).
    assert stats["p_one_tailed"] > 0.05


def test_welch_t_test_returns_small_p_when_test_clearly_better() -> None:
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    # Piccolo rumore per evitare il branch SE=0 (che ritorna p=1.0 by design).
    rng = random.Random(123)
    control = [-1.0 + rng.uniform(-0.05, 0.05) for _ in range(30)]
    test = [1.0 + rng.uniform(-0.05, 0.05) for _ in range(30)]
    stats = tuner._welch_t_test(control, test)
    # Separazione di ~2 unità con std ≈ 0.03 → p << 0.001
    assert stats["p_one_tailed"] < 0.001
    assert stats["mean_test"] > stats["mean_control"]


def test_ab_mode_completes_after_all_steps() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": None,
            "step_history": [
                {"key": "regime_filter", "status": "promoted"},
                {"key": "mtf_confluence", "status": "rejected"},
            ],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.run(db)

    assert result["status"] == "completed"
