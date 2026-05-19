from datetime import datetime, timezone
from decimal import Decimal

from app.api.routes import _calculate_unrealized_pnl
from app.core.config import Settings
from app.db.models import AppSetting, Candle, DecisionLog, Order, Position, Signal
from app.engine.execution import OrderExecutionEngine


class FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def filter(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None


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


class FakePaperDb:
    def __init__(
        self,
        *,
        existing_order: Order | None = None,
        existing_position: Position | None = None,
    ) -> None:
        self.existing_order = existing_order
        self.existing_position = existing_position
        self.added: list[object] = []
        self.committed = 0
        self.next_position_id = 1

    def get(self, model: type[object], row_id: object) -> object | None:
        if model is Position and self.existing_position is not None:
            if self.existing_position.id == row_id:
                return self.existing_position
        if model is AppSetting:
            return None
        return None

    def query(self, model: type[object], *_args, **_kwargs) -> FakeQuery:
        if model is Order and self.existing_order is not None:
            return FakeQuery([self.existing_order])
        return FakeQuery([])

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, Position) and row.id is None:
            row.id = self.next_position_id
            self.next_position_id += 1

    def flush(self) -> None:
        for row in self.added:
            if isinstance(row, Position) and row.id is None:
                row.id = self.next_position_id
                self.next_position_id += 1

    def commit(self) -> None:
        self.committed += 1

    def refresh(self, _row: object) -> None:
        return None

    def added_of(self, model: type[object]) -> list[object]:
        return [row for row in self.added if isinstance(row, model)]


def _paper_signal(signal_id: int = 42) -> Signal:
    return Signal(
        id=signal_id,
        market_id=1,
        timeframe="5m",
        direction="LONG",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit_1=Decimal("105"),
        take_profit_2=Decimal("110"),
        take_profit_3=Decimal("115"),
        risk_reward=Decimal("2"),
        reason="test",
        strategy_breakdown={},
        status="new",
    )


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


def test_execute_paper_opens_position_even_if_same_market_already_has_open_paper() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = _paper_signal()
    db = FakePaperDb(
        existing_position=Position(
            id=99,
            market_id=1,
            mode="paper",
            side="long",
            entry_price=Decimal("100"),
            size=Decimal("1"),
            status="open",
            metadata_json={"source_signal_id": "1"},
        )
    )

    position = engine.execute_paper(db, signal)  # type: ignore[arg-type]

    assert position is not None
    assert position.id != 99
    assert position.metadata_json["source_signal_id"] == signal.id
    assert len(db.added_of(Position)) == 1
    assert len(db.added_of(Order)) == 1
    events = [log.event_type for log in db.added_of(DecisionLog)]
    assert events == ["paper_order_executed"]


def test_execute_paper_is_idempotent_for_same_signal() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = _paper_signal()
    existing_position = Position(
        id=99,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        metadata_json={"source_signal_id": str(signal.id)},
    )
    existing_order = Order(
        id=123,
        position_id=existing_position.id,
        signal_id=signal.id,
        mode="paper",
        order_type="market",
        side="long",
        price=Decimal("100"),
        size=Decimal("1"),
        status="filled",
        reduce_only=False,
        raw_response={},
    )
    db = FakePaperDb(existing_order=existing_order, existing_position=existing_position)

    position = engine.execute_paper(db, signal)  # type: ignore[arg-type]

    assert position is existing_position
    assert db.added == []
    assert db.committed == 0


def test_take_profit_plan_omits_non_positive_signal_targets() -> None:
    engine = OrderExecutionEngine(Settings())
    engine.position_manager.build_take_profit_plan = lambda db, signal: []  # type: ignore[method-assign]
    signal = _paper_signal()
    signal.take_profit_3 = Decimal("-0.01")

    plan = engine._build_take_profit_plan(FakePaperDb(), signal)  # type: ignore[arg-type]

    assert [item["level"] for item in plan] == ["tp1", "tp2"]


def test_existing_take_profit_plan_ignores_non_positive_short_target() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=10,
        market_id=1,
        mode="paper",
        side="short",
        entry_price=Decimal("0.50"),
        size=Decimal("1"),
        status="open",
        metadata_json={
            "take_profit_plan": [
                {"level": "tp1", "price": "0.40", "allocation": "0.50", "filled": False},
                {"level": "tp2", "price": "-0.05", "allocation": "0.30", "filled": False},
                {"level": "runner", "price": "", "allocation": "0.20", "filled": False, "runner": True},
            ]
        },
    )

    plan = engine._take_profit_plan(position)

    assert [item["level"] for item in plan] == ["tp1", "runner"]


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
