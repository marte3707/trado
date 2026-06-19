from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import AppSetting, DecisionLog, Position, Signal
from app.engine.portfolio_guard import PortfolioGuard


@dataclass
class FakeQuery:
    rows: list[Any]

    def filter(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        rows = list(self.rows)
        for expr in _args:
            key = getattr(getattr(expr, "left", None), "key", None)
            operator = getattr(getattr(expr, "operator", None), "__name__", None)
            value = getattr(getattr(expr, "right", None), "value", None)
            if key is None or operator is None:
                continue
            if operator == "eq":
                rows = [row for row in rows if getattr(row, key) == value]
            elif operator == "ge":
                rows = [row for row in rows if getattr(row, key) >= value]
            elif operator == "is_not":
                rows = [row for row in rows if getattr(row, key) is not value]
        return FakeQuery(rows)

    def order_by(self, *args: Any) -> FakeQuery:
        rows = list(self.rows)
        for expr in args:
            key = getattr(getattr(expr, "element", None), "key", None)
            modifier = getattr(getattr(expr, "modifier", None), "__name__", None)
            if key is None:
                continue
            rows = sorted(rows, key=lambda row: getattr(row, key), reverse=modifier == "desc_op")
        return FakeQuery(rows)

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return list(self.rows)


class FakeDb:
    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        decision_logs: list[DecisionLog] | None = None,
        risk: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.positions = positions or []
        self.decision_logs = decision_logs or []
        self.rows: dict[str, AppSetting] = {
            "risk": AppSetting(key="risk", value=risk or {}),
            "portfolio_circuit_breaker_state": AppSetting(
                key="portfolio_circuit_breaker_state",
                value=state or {},
            ),
        }
        self.added: list[Any] = []

    def get(self, model: type[Any], key: Any) -> Any | None:
        if model is AppSetting:
            return self.rows.get(str(key))
        return None

    def query(self, model: type[Any], *_args: Any, **_kwargs: Any) -> FakeQuery:
        if model is Position:
            return FakeQuery(self.positions)
        if model is DecisionLog:
            return FakeQuery(self.decision_logs)
        return FakeQuery([])

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, AppSetting):
            self.rows[row.key] = row
        elif isinstance(row, DecisionLog):
            self.decision_logs.append(row)


def _signal(*, market_id: int = 99, timeframe: str = "5m", direction: str = "SHORT") -> Signal:
    return Signal(
        id=700 + market_id,
        market_id=market_id,
        timeframe=timeframe,
        direction=direction,
        score=82,
        confidence_label="high",
        entry_price=Decimal("100"),
        stop_loss=Decimal("105") if "SHORT" in direction else Decimal("95"),
        take_profit_1=Decimal("95"),
        take_profit_2=Decimal("90"),
        take_profit_3=Decimal("85"),
        risk_reward=Decimal("2"),
        reason="test",
        strategy_breakdown={},
        status="new",
    )


def _open_position(
    *,
    market_id: int,
    mode: str = "live",
    side: str = "short",
    timeframe: str = "5m",
    opened_at: datetime | None = None,
) -> Position:
    return Position(
        id=market_id,
        market_id=market_id,
        mode=mode,
        side=side,
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("105") if side == "short" else Decimal("95"),
        status="open",
        opened_at=opened_at or datetime(2026, 6, 7, 10, 0, tzinfo=UTC),
        metadata_json={
            "source_timeframe": timeframe,
            "initial_size": "1",
            "initial_position_risk": "100",
        },
    )


def _closed_stop(
    *,
    closed_at: datetime,
    mode: str = "live",
    side: str = "short",
    timeframe: str = "5m",
    market_id: int = 1,
) -> Position:
    return Position(
        id=market_id,
        market_id=market_id,
        mode=mode,
        side=side,
        entry_price=Decimal("100"),
        size=Decimal("0"),
        stop_loss=Decimal("105") if side == "short" else Decimal("95"),
        status="closed",
        closed_at=closed_at,
        realized_pnl=Decimal("-100"),
        metadata_json={
            "source_timeframe": timeframe,
            "exit_reason": "stop_loss",
            "initial_position_risk": "100",
        },
    )


def _risk(**portfolio_overrides: Any) -> dict[str, Any]:
    portfolio_guard = {
        "enabled": True,
        "max_new_positions_per_cycle_by_side": {"live": 3},
        "max_directional_open_positions": {"paper": 25, "live": 99},
        "max_directional_markets": {"live": 99},
        "max_directional_risk_r": {"paper": 25, "live": 99},
        "max_low_tf_directional_positions": {"timeframes": ["5m", "15m"], "paper": 25},
        "max_low_tf_directional_open_positions": {
            "apply_timeframes": ["5m", "15m"],
            "live": 8,
        },
    }
    portfolio_guard.update(portfolio_overrides)
    return {
        "portfolio_guard": portfolio_guard,
        "circuit_breaker": {
            "enabled": True,
            "window_minutes": 120,
            "max_loss_r": 99,
            "max_stop_losses": 99,
            "pause_minutes": 180,
        },
        "directional_reversal_breaker": {
            "enabled": True,
            "modes": {"paper": False, "live": True},
            "apply_timeframes": ["5m", "15m"],
            "window_minutes": 120,
            "stop_loss_count": 4,
            "cooldown_minutes": 180,
        },
    }


def test_low_tf_directional_open_cap_blocks_live_but_not_paper_by_default() -> None:
    positions = [_open_position(market_id=i, mode="live", side="short") for i in range(1, 9)]
    db = FakeDb(positions=positions, risk=_risk())
    guard = PortfolioGuard(Settings())

    live_decision = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=9, timeframe="5m", direction="SHORT"),
        mode="live",
        side="short",
        now=datetime(2026, 6, 7, 10, 30, tzinfo=UTC),
    )
    paper_decision = guard.evaluate_entry(
        FakeDb(
            positions=[
                _open_position(market_id=i, mode="paper", side="short")
                for i in range(1, 9)
            ],
            risk=_risk(),
        ),  # type: ignore[arg-type]
        _signal(market_id=9, timeframe="5m", direction="SHORT"),
        mode="paper",
        side="short",
        now=datetime(2026, 6, 7, 10, 30, tzinfo=UTC),
    )

    assert not live_decision.allowed
    assert live_decision.reason == "max low-timeframe directional positions reached"
    assert paper_decision.allowed


def test_directional_markets_cap_blocks_live_same_side_market_breadth() -> None:
    positions = [
        _open_position(market_id=i, mode="live", side="long", timeframe="1h")
        for i in range(1, 11)
    ]
    db = FakeDb(
        positions=positions,
        risk=_risk(max_directional_markets={"live": 10}),
    )

    decision = PortfolioGuard(Settings()).evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=11, timeframe="1h", direction="LONG"),
        mode="live",
        side="long",
        now=datetime(2026, 6, 7, 10, 30, tzinfo=UTC),
    )

    assert not decision.allowed
    assert decision.reason == "max directional markets reached"


def test_new_live_positions_per_cycle_by_side_blocks_fourth_same_side_entry() -> None:
    cycle_start = datetime(2026, 6, 7, 10, 0, tzinfo=UTC)
    decision_logs = [
        DecisionLog(
            event_type="worker_cycle",
            timestamp=cycle_start,
            message="previous cycle",
            data={},
        )
    ]
    positions = [
        _open_position(
            market_id=i,
            mode="live",
            side="short",
            timeframe="1h",
            opened_at=cycle_start + timedelta(minutes=i),
        )
        for i in range(1, 4)
    ]
    db = FakeDb(positions=positions, decision_logs=decision_logs, risk=_risk())

    decision = PortfolioGuard(Settings()).evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=4, timeframe="1h", direction="SHORT"),
        mode="live",
        side="short",
        now=cycle_start + timedelta(minutes=20),
    )

    assert not decision.allowed
    assert decision.reason == "max new directional positions per cycle reached"


def test_directional_reversal_breaker_blocks_same_side_live_after_june_7_stop_cluster() -> None:
    now = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
    positions = [
        _closed_stop(closed_at=now - timedelta(minutes=minutes), market_id=index)
        for index, minutes in enumerate((15, 35, 65, 95), start=1)
    ]
    db = FakeDb(positions=positions, risk=_risk())
    guard = PortfolioGuard(Settings())

    result = guard.update_circuit_breaker(db, now=now)  # type: ignore[arg-type]
    blocked = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=20, timeframe="5m", direction="SHORT"),
        mode="live",
        side="short",
        now=now,
    )
    opposite_side = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=21, timeframe="5m", direction="LONG"),
        mode="live",
        side="long",
        now=now,
    )
    paper_same_side = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=22, timeframe="5m", direction="SHORT"),
        mode="paper",
        side="short",
        now=now,
    )
    higher_tf_same_side = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(market_id=23, timeframe="1h", direction="SHORT"),
        mode="live",
        side="short",
        now=now,
    )

    assert result["directional_reversal"]["status"] == "activated"
    assert not blocked.allowed
    assert blocked.reason == "directional reversal breaker active"
    assert opposite_side.allowed
    assert paper_same_side.allowed
    assert higher_tf_same_side.allowed
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "directional_reversal_breaker_activated"
        for row in db.added
    )
