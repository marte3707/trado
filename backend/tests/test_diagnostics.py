from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import AppSetting, DecisionLog, Position, Signal
from app.engine.diagnostics import (
    build_equity_slope_diagnostics,
    consecutive_zero_signal_cycles,
    equity_slope,
    exit_reason_distribution,
    planned_stop_pct_summary,
)


def _position(
    *,
    pnl: float | None,
    closed_at: datetime | None,
    exit_reason: str | None = None,
    source_signal_id: int | None = None,
    source_timeframe: str | None = None,
) -> Position:
    metadata: dict = {}
    if exit_reason is not None:
        metadata["exit_reason"] = exit_reason
    if source_signal_id is not None:
        metadata["source_signal_id"] = source_signal_id
    if source_timeframe is not None:
        metadata["source_timeframe"] = source_timeframe
    position = Position(
        market_id=1,
        mode="paper",
        side="LONG",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="closed",
    )
    position.realized_pnl = Decimal(str(pnl)) if pnl is not None else None
    position.closed_at = closed_at
    position.metadata_json = metadata
    return position


def _signal(signal_id: int, *, entry: float, stop: float) -> Signal:
    signal = Signal(
        market_id=1,
        timeframe="15m",
        direction="LONG",
        score=70,
        confidence_label="medium",
        entry_price=Decimal(str(entry)),
        reason="test",
    )
    signal.id = signal_id
    signal.stop_loss = Decimal(str(stop))
    return signal


class FakeQuery:
    def __init__(self, rows: list[DecisionLog]) -> None:
        self.rows = rows

    def filter(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def limit(self, value: int):
        self.rows = self.rows[:value]
        return self

    def all(self):
        return self.rows


class FakeDiagnosticsDb:
    def __init__(self, positions: list[Position], logs: list[DecisionLog]) -> None:
        self.positions = positions
        self.logs = logs
        self.settings = {
            "risk": AppSetting(key="risk", value={"min_signal_score": 72}),
        }

    def get(self, model, key):  # noqa: ANN001
        if model is AppSetting:
            return self.settings.get(key)
        return None

    def query(self, model):  # noqa: ANN001
        assert model is DecisionLog
        return FakeQuery(self.logs)


def test_exit_reason_distribution_groups_reasons_and_handles_missing() -> None:
    positions = [
        _position(pnl=-5, closed_at=None, exit_reason="stop_loss"),
        _position(pnl=10, closed_at=None, exit_reason="take_profit"),
        _position(pnl=-3, closed_at=None, exit_reason="stop_loss"),
        _position(pnl=0, closed_at=None, exit_reason=None),
    ]

    distribution = exit_reason_distribution(positions)

    assert distribution == {"stop_loss": 2, "take_profit": 1, "unknown": 1}


def test_planned_stop_pct_summary_computes_percentiles_from_linked_signals() -> None:
    signals_by_id = {
        1: _signal(1, entry=100.0, stop=99.0),   # 1.0%
        2: _signal(2, entry=100.0, stop=98.0),   # 2.0%
        3: _signal(3, entry=100.0, stop=97.0),   # 3.0%
        4: _signal(4, entry=100.0, stop=96.0),   # 4.0%
        5: _signal(5, entry=100.0, stop=95.0),   # 5.0%
    }
    positions = [
        _position(pnl=-1, closed_at=None, source_signal_id=1),
        _position(pnl=-2, closed_at=None, source_signal_id=2),
        _position(pnl=-3, closed_at=None, source_signal_id=3),
        _position(pnl=-4, closed_at=None, source_signal_id=4),
        _position(pnl=-5, closed_at=None, source_signal_id=5),
        _position(pnl=-1, closed_at=None, source_signal_id=99),  # signal mancante, ignorato
    ]

    summary = planned_stop_pct_summary(positions, signals_by_id)

    assert summary["count"] == 5
    # nearest-rank percentiles su [0.01, 0.02, 0.03, 0.04, 0.05]
    assert summary["p25"] == 0.02
    assert summary["p50"] == 0.03
    assert summary["p75"] == 0.04
    assert summary["p90"] == 0.05


def test_planned_stop_pct_summary_returns_none_when_empty() -> None:
    summary = planned_stop_pct_summary([], {})

    assert summary["count"] == 0
    assert summary["p50"] is None


def test_equity_slope_is_negative_for_declining_pnl() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    positions = [
        _position(pnl=-1.0, closed_at=base + timedelta(minutes=i))
        for i in range(12)
    ]

    slope = equity_slope(positions)

    assert slope < 0


def test_equity_slope_is_positive_for_growing_pnl() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    positions = [
        _position(pnl=2.0, closed_at=base + timedelta(minutes=i))
        for i in range(12)
    ]

    slope = equity_slope(positions)

    assert slope > 0


def test_equity_slope_zero_when_insufficient_history() -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    positions = [
        _position(pnl=-1.0, closed_at=base + timedelta(minutes=i))
        for i in range(5)
    ]

    assert equity_slope(positions) == 0.0


def test_consecutive_zero_signal_cycles_uses_live_worker_logs() -> None:
    rows = [
        DecisionLog(event_type="worker_cycle_failed", message="failed", data={}),
        DecisionLog(
            event_type="worker_cycle",
            message="no-op",
            data={"active_timeframes": [], "generated": 0},
        ),
        DecisionLog(
            event_type="worker_cycle",
            message="zero",
            data={"active_timeframes": ["15m"], "generated": 0},
        ),
        DecisionLog(
            event_type="worker_cycle",
            message="zero",
            data={"active_timeframes": ["15m"], "generated": 0},
        ),
        DecisionLog(
            event_type="worker_cycle",
            message="generated",
            data={"active_timeframes": ["15m"], "generated": 1},
        ),
    ]
    db = FakeDiagnosticsDb([], rows)

    assert consecutive_zero_signal_cycles(db) == 3  # type: ignore[arg-type]


def test_equity_slope_diagnostics_uses_live_zero_cycle_count(monkeypatch) -> None:
    rows = [
        DecisionLog(
            event_type="worker_cycle",
            message="zero",
            data={"active_timeframes": ["15m"], "generated": 0},
        ),
        DecisionLog(
            event_type="worker_cycle",
            message="zero",
            data={"active_timeframes": ["15m"], "generated": 0},
        ),
        DecisionLog(
            event_type="worker_cycle",
            message="generated",
            data={"active_timeframes": ["15m"], "generated": 1},
        ),
    ]
    db = FakeDiagnosticsDb([], rows)
    monkeypatch.setattr(
        "app.engine.diagnostics.SignalPerformanceAnalyzer.closed_paper_positions",
        lambda self, db, limit=500, **kwargs: [],
    )

    diagnostics = build_equity_slope_diagnostics(db, lookback=500)  # type: ignore[arg-type]

    assert diagnostics["consecutive_zero_signal_cycles"] == 2


def test_build_equity_slope_diagnostics_exposes_status_and_time_stop_impact(monkeypatch) -> None:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    positions = [
        _position(pnl=-2.0, closed_at=base + timedelta(minutes=i))
        for i in range(60)
    ]
    log = DecisionLog(
        event_type="paper_position_time_stop_closed",
        message="closed",
        data={"realized_pnl": "-12.5"},
    )
    log.timestamp = datetime.now(UTC)
    db = FakeDiagnosticsDb(positions, [log])
    monkeypatch.setattr(
        "app.engine.diagnostics.SignalPerformanceAnalyzer.closed_paper_positions",
        lambda self, db, limit=500, **kwargs: positions[:limit],
    )

    diagnostics = build_equity_slope_diagnostics(db, lookback=500)  # type: ignore[arg-type]

    assert diagnostics["status"] == "declining"
    assert diagnostics["threshold"] == -0.1
    assert diagnostics["recent_time_stop_impact"]["closed_count"] == 1
    assert diagnostics["recent_time_stop_impact"]["realized_pnl"] == -12.5
    assert "deep_defensive_active" not in diagnostics
    assert "baseline_vs_current" not in diagnostics
