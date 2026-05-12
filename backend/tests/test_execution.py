from datetime import datetime, timezone
from decimal import Decimal

from app.api.routes import _calculate_unrealized_pnl
from app.core.config import Settings
from app.db.models import Candle, Order, Position, Signal
from app.engine.execution import OrderExecutionEngine


class FakeDb:
    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.added: list[object] = []
        self.committed = False

    def get(self, model: type[object], row_id: int) -> object | None:
        if model is Signal and row_id == self.signal.id:
            return self.signal
        return None

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed = True


def test_candle_containing_position_open_can_trigger_exit() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        status="open",
        opened_at=datetime(2026, 1, 1, 12, 2, tzinfo=timezone.utc),
        metadata_json={},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("111"),
        low=Decimal("99"),
        close=Decimal("110"),
        volume=Decimal("1"),
    )

    assert engine._candle_can_affect_position(position, candle, "5m")
    assert engine._paper_trigger_for_candle(position, candle) == ("take_profit", Decimal("110"))


def test_candle_closed_before_position_open_is_ignored() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        status="open",
        opened_at=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
        metadata_json={},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("111"),
        low=Decimal("99"),
        close=Decimal("110"),
        volume=Decimal("1"),
    )

    assert not engine._candle_can_affect_position(position, candle, "5m")


def test_unrealized_pnl_uses_latest_mark_for_long_position() -> None:
    position = Position(
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("2"),
        status="open",
        metadata_json={},
    )

    assert _calculate_unrealized_pnl(position, Decimal("103.5")) == Decimal("7.0")


def test_unrealized_pnl_uses_latest_mark_for_short_position() -> None:
    position = Position(
        market_id=1,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("2"),
        status="open",
        metadata_json={},
    )

    assert _calculate_unrealized_pnl(position, Decimal("97.5")) == Decimal("5.0")


def test_closing_position_marks_source_signal_closed() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = Signal(
        id=379,
        market_id=1,
        timeframe="5m",
        direction="LONG",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        reason="test",
        strategy_breakdown={},
        status="executed",
    )
    position = Position(
        id=10,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        metadata_json={"source_signal_id": "379"},
    )
    db = FakeDb(signal)

    engine.close_position(db, position, Decimal("110"), "take_profit")  # type: ignore[arg-type]

    assert position.status == "closed"
    assert signal.status == "closed"
    assert db.committed


def test_take_profit_plan_closes_half_at_tp1() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = Signal(id=11, market_id=1, timeframe="5m", direction="LONG", score=80, confidence_label="high", entry_price=Decimal("100"), reason="test", strategy_breakdown={})
    position = Position(
        id=10,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("10"),
        stop_loss=Decimal("95"),
        status="open",
        opened_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        metadata_json={
            "source_signal_id": "11",
            "initial_size": "10",
            "take_profit_plan": [
                {"level": "tp1", "price": "105", "allocation": "0.50", "filled": False},
                {"level": "tp2", "price": "110", "allocation": "0.30", "filled": False},
                {"level": "tp3", "price": "115", "allocation": "0.20", "filled": False},
            ],
        },
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("104"),
        volume=Decimal("1"),
    )
    db = FakeDb(signal)

    assert engine._apply_paper_triggers_for_candle(db, position, candle) == (1, 0)  # type: ignore[arg-type]

    exit_orders = [item for item in db.added if isinstance(item, Order)]
    assert position.status == "open"
    assert position.size == Decimal("5.00")
    assert position.realized_pnl == Decimal("25.00")
    assert exit_orders[0].size == Decimal("5.00")
    assert position.metadata_json["take_profit_plan"][0]["filled"] is True


def test_take_profit_plan_can_fill_all_targets_in_one_candle() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = Signal(id=12, market_id=1, timeframe="5m", direction="LONG", score=80, confidence_label="high", entry_price=Decimal("100"), reason="test", strategy_breakdown={}, status="executed")
    position = Position(
        id=10,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("10"),
        stop_loss=Decimal("95"),
        status="open",
        opened_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        metadata_json={
            "source_signal_id": "12",
            "initial_size": "10",
            "take_profit_plan": [
                {"level": "tp1", "price": "105", "allocation": "0.50", "filled": False},
                {"level": "tp2", "price": "110", "allocation": "0.30", "filled": False},
                {"level": "tp3", "price": "115", "allocation": "0.20", "filled": False},
            ],
        },
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("116"),
        low=Decimal("99"),
        close=Decimal("115"),
        volume=Decimal("1"),
    )
    db = FakeDb(signal)

    assert engine._apply_paper_triggers_for_candle(db, position, candle) == (3, 1)  # type: ignore[arg-type]

    exit_orders = [item for item in db.added if isinstance(item, Order)]
    assert position.status == "closed"
    assert signal.status == "closed"
    assert position.size == Decimal("0")
    assert position.realized_pnl == Decimal("85.00")
    assert [order.size for order in exit_orders] == [Decimal("5.00"), Decimal("3.00"), Decimal("2.00")]
