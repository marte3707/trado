import random
from datetime import UTC, datetime, timedelta
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
    source_timeframe: str | None = None,
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
    if source_timeframe is not None:
        meta["source_timeframe"] = source_timeframe
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
    positions = _positions_with_slope(20, -1.0, datetime(2026, 5, 14, tzinfo=UTC))
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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


def test_status_next_step_skips_active_step() -> None:
    db = FakeDb()
    sequence = [
        {"key": "regime_filter", "patch": {}, "revert": {}},
        {"key": "mtf_confluence", "patch": {}, "revert": {}},
        {"key": "volatility_filter", "patch": {}, "revert": {}},
        {"key": "participation_ratio_50", "patch": {}, "revert": {}},
    ]
    db.app_settings["auto_tuner_config"] = _config(
        mode="ab_test",
        sequence=sequence,
        ab_test_min_trades_per_arm=30,
    )
    activated = datetime(2026, 5, 22, 15, 20, tzinfo=UTC)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 2,
            "current_step_key": "volatility_filter",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {"volatility_filter_enabled": False},
                "test_overlay": {"volatility_filter_enabled": True},
                "allocation": 0.5,
            },
            "step_history": [
                {"key": "regime_filter", "status": "success"},
                {"key": "mtf_confluence", "status": "reverted"},
            ],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    status = tuner.status(db)

    assert status["current_step_key"] == "volatility_filter"
    assert status["next_step_key"] == "participation_ratio_50"


def test_positions_after_returns_empty_when_state_corrupt() -> None:
    """F-10: senza `activated_at` ritorniamo lista vuota, non l'intera collezione.
    Altrimenti `metrics_after` includerebbe le posizioni pre-attivazione."""
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    positions = _positions_with_slope(5, 1.0, datetime(2026, 1, 1, tzinfo=UTC))
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
    count: int,
    pnl: float,
    start: datetime,
    arm: str,
    step_key: str = "regime_filter",
    source_timeframe: str | None = None,
) -> list[Position]:
    return [
        _position(
            pnl=pnl,
            closed_at=start + timedelta(minutes=i),
            arm=arm,
            step_key=step_key,
            source_timeframe=source_timeframe,
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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


def test_strategy_ab_partition_only_counts_target_timeframe() -> None:
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
    step_key = "strategy_test:15m:volume_profile"
    positions = [
        _position(
            pnl=1.0,
            closed_at=activated + timedelta(minutes=1),
            arm="control",
            step_key=step_key,
            source_timeframe="15m",
        ),
        _position(
            pnl=2.0,
            closed_at=activated + timedelta(minutes=2),
            arm="test",
            step_key=step_key,
            source_timeframe="4h",
        ),
        _position(
            pnl=3.0,
            closed_at=activated + timedelta(minutes=3),
            arm="test",
            step_key=step_key,
        ),
    ]

    arms = tuner._partition_by_arm(positions, activated.isoformat(), step_key)

    assert len(arms["control"]) == 1
    assert arms["control"][0].realized_pnl == Decimal("1.0")
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


def test_strategy_ab_overlay_only_applies_to_target_timeframe() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(ab_test_assignment_salt="tf-test")
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "strategy_test:15m:volume_profile",
            "mode": "ab_test",
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {
                    "disabled_strategies_by_timeframe": {"15m": ["volume_profile"]}
                },
                "allocation": 1.0,
            },
        },
    )
    candle_time = datetime(2026, 5, 14, 12, tzinfo=UTC)

    assert active_ab_overlay(
        db,
        market_id=1,
        timeframe="4h",
        source_candle_open_time=candle_time,
    ) is None
    overlay = active_ab_overlay(
        db,
        market_id=1,
        timeframe="15m",
        source_candle_open_time=candle_time,
    )

    assert overlay is not None
    assert overlay["step_key"] == "strategy_test:15m:volume_profile"
    assert overlay["arm"] == "test"
    assert overlay["overlay"] == {
        "disabled_strategies_by_timeframe": {"15m": ["volume_profile"]}
    }


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
    candle_time = datetime(2026, 5, 14, 12, tzinfo=UTC)

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
    base = datetime(2026, 5, 14, 12, tzinfo=UTC)

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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
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


def test_strategy_tests_lists_only_supported_pairs_and_last_recommendation() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(enabled=False)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "step_history": [
                {
                    "key": "strategy_test:5m:cvd_divergence",
                    "status": "evaluated",
                    "ab_test": {"recommendation": "candidate_disable"},
                }
            ]
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.strategy_tests(db)

    assert result["blocked"] is False
    pairs = {(row["timeframe"], row["strategy"]) for row in result["tests"]}
    assert pairs == {
        ("5m", "cvd_divergence"),
        ("5m", "volume_profile"),
        ("5m", "trend_following_adaptive"),
        ("15m", "cvd_divergence"),
        ("15m", "volume_profile"),
        ("15m", "trend_following_adaptive"),
    }
    row = next(
        item
        for item in result["tests"]
        if item["timeframe"] == "5m" and item["strategy"] == "cvd_divergence"
    )
    assert row["last_recommendation"] == "candidate_disable"
    assert row["queued"] is False


def test_start_strategy_test_creates_dynamic_ab_sequence_without_mutating_risk() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(enabled=False)
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"min_signal_score": 70}
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.start_strategy_test(db, "5m", "cvd_divergence")

    assert result["ok"] is True
    assert result["mode"] == "ab_test"
    assert result["step"] == "strategy_test:5m:cvd_divergence"
    config = db.app_settings["auto_tuner_config"].value
    assert config["enabled"] is True
    assert config["mode"] == "ab_test"
    assert config["sequence"][0]["strategy_test"] == {
        "timeframe": "5m",
        "strategy": "cvd_divergence",
    }
    assert db.app_settings["risk"].value == {"min_signal_score": 70}
    state = db.app_settings["auto_tuner_state"].value
    assert state["ab_test"]["control_overlay"] == {}
    assert state["ab_test"]["test_overlay"] == {
        "disabled_strategies_by_timeframe": {"5m": ["cvd_divergence"]}
    }


def test_start_candidate_strategy_test_enables_only_test_arm() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(enabled=False)
    db.app_settings["risk"] = AppSetting(key="risk", value={"min_signal_score": 70})
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.start_strategy_test(db, "15m", "trend-following-adaptive")

    assert result["ok"] is True
    assert result["mode"] == "ab_test"
    assert result["step"] == "strategy_test:15m:trend_following_adaptive"
    state = db.app_settings["auto_tuner_state"].value
    assert state["ab_test"]["control_overlay"] == {}
    assert state["ab_test"]["test_overlay"] == {
        "enabled_strategies_by_timeframe": {"15m": ["trend_following_adaptive"]}
    }


def test_start_strategy_test_rejects_unsupported_pair() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(enabled=False)
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.start_strategy_test(db, "1h", "cvd_divergence")

    assert result["ok"] is False
    assert result["status"] == "unsupported_strategy_test"


def test_ab_strategy_test_evaluates_without_mutating_risk_and_sets_recommendation() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config(
        ab_test_min_trades_per_arm=10,
        ab_test_significance_level=0.05,
    )
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "strategy_test:5m:cvd_divergence",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {
                    "disabled_strategies_by_timeframe": {"5m": ["cvd_divergence"]}
                },
                "allocation": 0.5,
            },
            "step_history": [],
        },
    )
    db.app_settings["auto_tuner_config"].value["sequence"] = [
        {
            "key": "strategy_test:5m:cvd_divergence",
            "label": "Strategy test 5m/cvd_divergence",
            "patch": {
                "disabled_strategies_by_timeframe": {"5m": ["cvd_divergence"]}
            },
            "revert": {},
            "strategy_test": {"timeframe": "5m", "strategy": "cvd_divergence"},
        }
    ]
    db.app_settings["risk"] = AppSetting(
        key="risk", value={"regime_filter_enabled": False}
    )
    rng = random.Random(17)
    control_positions = [
        _position(
            pnl=-4.0 + rng.uniform(-0.3, 0.3),
            closed_at=activated + timedelta(minutes=i + 1),
            arm="control",
            step_key="strategy_test:5m:cvd_divergence",
            source_timeframe="5m",
        )
        for i in range(20)
    ]
    test_positions = [
        _position(
            pnl=3.0 + rng.uniform(-0.3, 0.3),
            closed_at=activated + timedelta(minutes=i + 50),
            arm="test",
            step_key="strategy_test:5m:cvd_divergence",
            source_timeframe="5m",
        )
        for i in range(20)
    ]
    tuner = AutoTuner(analyzer=FakeAnalyzer(control_positions + test_positions))

    result = tuner.run(db)

    assert result["status"] == "evaluated"
    assert result["recommendation"] == "candidate_disable"
    assert db.app_settings["risk"].value["regime_filter_enabled"] is False
    history = db.app_settings["auto_tuner_state"].value["step_history"]
    assert history[0]["status"] == "evaluated"
    assert history[0]["ab_test"]["recommendation"] == "candidate_disable"


def test_status_exposes_active_strategy_test_target() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_config"].value["sequence"] = [
        {
            "key": "strategy_test:15m:volume_profile",
            "label": "Strategy test 15m/volume_profile",
            "patch": {
                "disabled_strategies_by_timeframe": {"15m": ["volume_profile"]}
            },
            "revert": {},
            "strategy_test": {"timeframe": "15m", "strategy": "volume_profile"},
        }
    ]
    activated = datetime(2026, 5, 14, 12, tzinfo=UTC)
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "current_step_index": 0,
            "current_step_key": "strategy_test:15m:volume_profile",
            "mode": "ab_test",
            "activated_at": activated.isoformat(),
            "trades_at_activation": 0,
            "ab_test": {
                "control_overlay": {},
                "test_overlay": {
                    "disabled_strategies_by_timeframe": {"15m": ["volume_profile"]}
                },
                "allocation": 0.5,
                "assignment_method": "deterministic_hash",
            },
            "step_history": [],
        },
    )
    positions = (
        _tagged_positions(
            4,
            -1.0,
            activated + timedelta(minutes=1),
            "control",
            step_key="strategy_test:15m:volume_profile",
            source_timeframe="15m",
        )
        + _tagged_positions(
            6,
            2.0,
            activated + timedelta(minutes=20),
            "test",
            step_key="strategy_test:15m:volume_profile",
            source_timeframe="15m",
        )
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer(positions))

    status = tuner.status(db)

    assert status["target_timeframe"] == "15m"
    assert status["target_strategy"] == "volume_profile"
    assert status["ab_progress"]["trades_control"] == 4
    assert status["ab_progress"]["trades_test"] == 6


def test_queue_strategy_test_persists_single_queued_item() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.queue_strategy_test(db, "5m", "cvd_divergence")

    assert result["ok"] is True
    assert result["status"] == "queued"
    state = db.app_settings["auto_tuner_state"].value
    assert state["queued_strategy_test"]["step_key"] == "strategy_test:5m:cvd_divergence"


def test_queue_strategy_test_rejects_second_different_queue() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "queued_strategy_test": {
                "timeframe": "5m",
                "strategy": "cvd_divergence",
                "step_key": "strategy_test:5m:cvd_divergence",
                "queued_at": "2026-06-08T10:00:00+00:00",
            },
            "step_history": [],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.queue_strategy_test(db, "15m", "volume_profile")

    assert result["ok"] is False
    assert result["status"] == "queue_conflict"
    assert result["queued_strategy_test"]["step_key"] == "strategy_test:5m:cvd_divergence"


def test_run_starts_queued_strategy_test_when_idle() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["risk"] = AppSetting(key="risk", value={"min_signal_score": 70})
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "queued_strategy_test": {
                "timeframe": "5m",
                "strategy": "cvd_divergence",
                "step_key": "strategy_test:5m:cvd_divergence",
                "queued_at": "2026-06-08T10:00:00+00:00",
            },
            "step_history": [],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.run(db)

    assert result["ok"] is True
    assert result["status"] == "started"
    assert result["queued"] is True
    state = db.app_settings["auto_tuner_state"].value
    assert state["current_step_key"] == "strategy_test:5m:cvd_divergence"
    assert state["queued_strategy_test"] is None


def test_clear_queued_strategy_test_removes_queue() -> None:
    db = FakeDb()
    db.app_settings["auto_tuner_config"] = _ab_config()
    db.app_settings["auto_tuner_state"] = AppSetting(
        key="auto_tuner_state",
        value={
            "queued_strategy_test": {
                "timeframe": "15m",
                "strategy": "volume_profile",
                "step_key": "strategy_test:15m:volume_profile",
                "queued_at": "2026-06-08T10:00:00+00:00",
            },
            "step_history": [],
        },
    )
    tuner = AutoTuner(analyzer=FakeAnalyzer([]))

    result = tuner.clear_queued_strategy_test(db)

    assert result["ok"] is True
    assert result["status"] == "queue_cleared"
    state = db.app_settings["auto_tuner_state"].value
    assert state["queued_strategy_test"] is None


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


# ---------------------------------------------------------------------------
# target_setting + sequential_only tests
# ---------------------------------------------------------------------------


def test_default_sequence_contains_auto_reverse() -> None:
    """Sanity check: lo step auto_reverse esiste nel DEFAULT_SEQUENCE e punta
    a btc_macro_protection con sequential_only=True."""
    from app.engine.auto_tuner import DEFAULT_SEQUENCE

    keys = [step["key"] for step in DEFAULT_SEQUENCE]
    assert "auto_reverse" in keys
    step = next(s for s in DEFAULT_SEQUENCE if s["key"] == "auto_reverse")
    assert step["target_setting"] == "btc_macro_protection"
    assert step["sequential_only"] is True
    assert step["patch"]["reverse_mode_enabled"] is True
    assert step["revert"]["reverse_mode_enabled"] is False


def test_step_with_custom_target_setting_applies_to_correct_setting() -> None:
    """Uno step con target_setting='btc_macro_protection' applica il patch a
    quella chiave, non a 'risk'."""
    db = FakeDb()
    sequence = [
        {
            "key": "auto_reverse",
            "label": "Auto-reverse",
            "target_setting": "btc_macro_protection",
            "patch": {"enabled": True, "reverse_mode_enabled": True},
            "revert": {"reverse_mode_enabled": False},
            "sequential_only": True,
        }
    ]
    db.app_settings["auto_tuner_config"] = AppSetting(
        key="auto_tuner_config",
        value={
            "enabled": True,
            "min_trades_per_step": 5,
            "improvement_threshold": 0.0,
            "sequence": sequence,
        },
    )
    db.app_settings["btc_macro_protection"] = AppSetting(
        key="btc_macro_protection",
        value={"enabled": False, "reverse_mode_enabled": False, "timeframe": "5m"},
    )
    db.app_settings["risk"] = AppSetting(key="risk", value={"min_signal_score": 70})

    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    result = tuner.run(db)

    assert result["status"] == "started"
    # Il patch è stato applicato a btc_macro_protection, NON a risk
    btc_value = db.app_settings["btc_macro_protection"].value
    assert btc_value["enabled"] is True
    assert btc_value["reverse_mode_enabled"] is True
    # risk è rimasto invariato
    risk_value = db.app_settings["risk"].value
    assert "reverse_mode_enabled" not in risk_value
    assert risk_value["min_signal_score"] == 70


def test_ab_mode_skips_sequential_only_steps() -> None:
    """In ab_test mode, gli step con sequential_only=True vengono saltati."""
    db = FakeDb()
    sequence = [
        {
            "key": "auto_reverse",
            "label": "Auto-reverse",
            "target_setting": "btc_macro_protection",
            "patch": {"reverse_mode_enabled": True},
            "revert": {"reverse_mode_enabled": False},
            "sequential_only": True,
        },
        {
            "key": "regime_filter",
            "label": "Regime",
            "patch": {"regime_filter_enabled": True},
            "revert": {"regime_filter_enabled": False},
        },
    ]
    db.app_settings["auto_tuner_config"] = AppSetting(
        key="auto_tuner_config",
        value={
            "enabled": True,
            "mode": "ab_test",
            "ab_test_min_trades_per_arm": 5,
            "ab_test_allocation": 0.5,
            "sequence": sequence,
        },
    )
    db.app_settings["risk"] = AppSetting(key="risk", value={"min_signal_score": 70})

    tuner = AutoTuner(analyzer=FakeAnalyzer([]))
    result = tuner.run(db)

    # Deve partire con regime_filter, saltando auto_reverse (sequential_only)
    assert result["status"] == "started"
    assert result["step"] == "regime_filter"
