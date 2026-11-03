from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import AppSetting, Position, Signal
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
            rows = sorted(
                rows,
                key=lambda row: getattr(row, key),
                reverse=modifier == "desc_op",
            )
        return FakeQuery(rows)

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self.rows[:count])

    def count(self) -> int:
        return len(self.rows)

    def all(self) -> list[Any]:
        return list(self.rows)


class FakeDb:
    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        settings: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.positions = positions or []
        self.settings = {
            key: AppSetting(key=key, value=value)
            for key, value in (settings or {}).items()
        }
        self.added: list[Any] = []

    def get(self, model: type[Any], key: Any) -> Any | None:
        if model is AppSetting:
            return self.settings.get(str(key))
        return None

    def query(self, model: type[Any], *_args: Any, **_kwargs: Any) -> FakeQuery:
        if model is Position or model is Position.id:
            return FakeQuery(self.positions)
        return FakeQuery([])

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, AppSetting):
            self.settings[row.key] = row


def _signal(*, timeframe: str = "5m") -> Signal:
    return Signal(
        id=42,
        market_id=1,
        timeframe=timeframe,
        direction="SHORT",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        stop_loss=Decimal("105"),
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
    mode: str = "paper",
    side: str = "short",
    timeframe: str = "5m",
) -> Position:
    return Position(
        id=1,
        market_id=1,
        mode=mode,
        side=side,
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("105") if side == "short" else Decimal("95"),
        status="open",
        metadata_json={
            "source_timeframe": timeframe,
            "initial_size": "1",
            "initial_position_risk": "100",
        },
    )


def _closed_position(*, closed_at: datetime) -> Position:
    return Position(
        id=1,
        market_id=1,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("0"),
        stop_loss=Decimal("105"),
        status="closed",
        closed_at=closed_at,
        realized_pnl=Decimal("-100"),
        metadata_json={
            "exit_reason": "stop_loss",
            "initial_position_risk": "100",
        },
    )


def _risk_settings(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "min_signal_score": 65,
        "portfolio_guard": {
            "enabled": True,
            "max_directional_open_positions": {"paper": 2, "live": 2},
            "max_directional_risk_r": {"paper": 99, "live": 99},
            "max_low_tf_directional_positions": {
                "timeframes": ["5m", "15m"],
                "paper": 99,
                "live": 99,
            },
        },
        "circuit_breaker": {
            "enabled": True,
            "window_minutes": 90,
            "max_loss_r": 5,
            "max_stop_losses": 8,
            "pause_minutes": 180,
            "scope": "new_entries_only",
        },
    }
    value.update(overrides)
    return value


def test_portfolio_guard_blocks_excessive_same_direction_positions() -> None:
    db = FakeDb(
        positions=[
            _open_position(mode="paper", side="short"),
            _open_position(mode="paper", side="short", timeframe="15m"),
        ],
        settings={"risk": _risk_settings()},
    )

    decision = PortfolioGuard(Settings()).evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(),
        mode="paper",
        side="short",
    )

    assert not decision.allowed
    assert decision.reason == "max directional open positions reached"


def test_portfolio_guard_keeps_paper_and_live_caps_separate() -> None:
    db = FakeDb(
        positions=[
            _open_position(mode="paper", side="short"),
            _open_position(mode="paper", side="short", timeframe="15m"),
        ],
        settings={"risk": _risk_settings()},
    )

    decision = PortfolioGuard(Settings()).evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(),
        mode="live",
        side="short",
    )

    assert decision.allowed


def test_portfolio_guard_blocks_low_timeframe_directional_cluster() -> None:
    risk = _risk_settings()
    risk["portfolio_guard"]["max_directional_open_positions"] = {
        "paper": 99,
        "live": 99,
    }
    risk["portfolio_guard"]["max_low_tf_directional_positions"] = {
        "timeframes": ["5m", "15m"],
        "paper": 2,
        "live": 99,
    }
    db = FakeDb(
        positions=[
            _open_position(mode="paper", side="short", timeframe="5m"),
            _open_position(mode="paper", side="short", timeframe="15m"),
        ],
        settings={"risk": risk},
    )

    decision = PortfolioGuard(Settings()).evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(timeframe="5m"),
        mode="paper",
        side="short",
    )

    assert not decision.allowed
    assert decision.reason == "max low-timeframe directional positions reached"


def test_circuit_breaker_activates_and_blocks_new_entries() -> None:
    now = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)
    risk = _risk_settings()
    risk["circuit_breaker"]["max_loss_r"] = 3
    db = FakeDb(
        positions=[
            _closed_position(closed_at=now - timedelta(minutes=10)),
            _closed_position(closed_at=now - timedelta(minutes=20)),
            _closed_position(closed_at=now - timedelta(minutes=30)),
            _closed_position(closed_at=now - timedelta(minutes=40)),
        ],
        settings={"risk": risk},
    )
    guard = PortfolioGuard(Settings())

    result = guard.update_circuit_breaker(db, now=now)  # type: ignore[arg-type]
    decision = guard.evaluate_entry(
        db,  # type: ignore[arg-type]
        _signal(),
        mode="paper",
        side="short",
        now=now,
    )

    assert result["status"] == "activated"
    assert not decision.allowed
    assert decision.reason == "portfolio circuit breaker active"


def test_new_market_probation_scales_size_for_market_without_history() -> None:
    risk = _risk_settings()
    risk["new_market_probation"] = {
        "enabled": True,
        "min_closed_trades": 3,
        "size_multiplier": 0.5,
    }
    db = FakeDb(positions=[], settings={"risk": risk})

    assert PortfolioGuard(Settings()).position_size_multiplier(
        db,  # type: ignore[arg-type]
        _signal(),
    ) == 0.5


def test_new_market_probation_full_size_once_history_built() -> None:
    risk = _risk_settings()
    risk["new_market_probation"] = {
        "enabled": True,
        "min_closed_trades": 3,
        "size_multiplier": 0.5,
    }
    now = datetime.now(UTC)
    closed = [
        _closed_position(closed_at=now - timedelta(hours=hour))
        for hour in range(3)
    ]
    db = FakeDb(positions=closed, settings={"risk": risk})

    assert PortfolioGuard(Settings()).position_size_multiplier(
        db,  # type: ignore[arg-type]
        _signal(),
    ) == 1.0


def test_new_market_probation_disabled_or_missing_keeps_full_size() -> None:
    disabled_risk = _risk_settings()
    disabled_risk["new_market_probation"] = {
        "enabled": False,
        "min_closed_trades": 3,
        "size_multiplier": 0.5,
    }

    for risk in (disabled_risk, _risk_settings()):
        db = FakeDb(positions=[], settings={"risk": risk})
        assert PortfolioGuard(Settings()).position_size_multiplier(
            db,  # type: ignore[arg-type]
            _signal(),
        ) == 1.0
