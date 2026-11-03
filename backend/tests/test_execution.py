from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.db.models import (
    AppSetting,
    Candle,
    DecisionLog,
    Market,
    MarketL2Snapshot,
    Order,
    Position,
    Signal,
)
from app.engine.execution import OrderExecutionEngine, _paper_l2_fill_price
from app.services.position_pnl import calculate_unrealized_pnl as _calculate_unrealized_pnl


class FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def filter(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def order_by(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def first(self) -> object | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[object]:
        return list(self.rows)


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
        book_enabled: bool = True,
        book: MarketL2Snapshot | None = None,
    ) -> None:
        self.existing_order = existing_order
        self.existing_position = existing_position
        self.added: list[object] = []
        self.committed = 0
        self.next_position_id = 1
        self.book = (
            book or MarketL2Snapshot(
                market_id=1,
                bids=[{"px": "99.9", "sz": "1000"}],
                asks=[{"px": "100", "sz": "1000"}],
                updated_at=datetime.now(UTC),
            )
        ) if book_enabled else None

    def get(self, model: type[object], row_id: object) -> object | None:
        if model is Position and self.existing_position is not None:
            if self.existing_position.id == row_id:
                return self.existing_position
        if model is AppSetting:
            return None
        if model is MarketL2Snapshot:
            return self.book
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


class FakeTimeStopDb:
    def __init__(
        self,
        *,
        positions: list[Position],
        candles: list[Candle] | None = None,
        position_management: dict | None = None,
    ) -> None:
        self.positions = positions
        self.candles = candles or []
        self.position_management = position_management or {}
        self.added: list[object] = []
        self.committed = 0

    def get(self, model: type[object], key: object) -> object | None:
        if model is AppSetting and key == "position_management":
            return AppSetting(key="position_management", value=self.position_management)
        if model is Signal:
            return None
        return None

    def query(self, model: type[object], *_args, **_kwargs) -> FakeQuery:
        if model is Position:
            return FakeQuery(self.positions)
        if model is Candle:
            return FakeQuery(self.candles)
        return FakeQuery([])

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1


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
        opened_at=datetime(2026, 1, 1, 12, 2, tzinfo=UTC),
        metadata_json={},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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
        opened_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
        metadata_json={},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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
    signal.strategy_breakdown = {
        "_experiment": {
            "version": "baseline-frozen-v1",
            "configuration_hash": "abc123",
        }
    }
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
    # Forward attribution: la posizione registra le soglie operative all'apertura.
    assert "risk_at_open" in position.metadata_json
    assert set(position.metadata_json["risk_at_open"]) == {
        "min_signal_score",
        "min_risk_reward",
        "max_risk_per_trade_pct",
    }
    assert position.metadata_json["experiment"] == signal.strategy_breakdown["_experiment"]
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


def test_execute_paper_uses_fresh_book_vwap_for_standard_signal() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = _paper_signal()
    book = MarketL2Snapshot(
        market_id=1,
        bids=[{"px": "99.9", "sz": "1000"}],
        asks=[{"px": "101", "sz": "0.01"}, {"px": "102", "sz": "1000"}],
        updated_at=datetime.now(UTC),
    )
    db = FakePaperDb(book=book)

    position = engine.execute_paper(db, signal)  # type: ignore[arg-type]

    assert position is not None
    assert Decimal("101") < position.entry_price < Decimal("102")
    risk_budget = (
        Decimal(position.metadata_json["account_value_at_entry"])
        * Decimal(str(Settings().max_risk_per_trade_pct))
        / Decimal("100")
    )
    assert position.size < risk_budget / (signal.entry_price - signal.stop_loss)
    assert Decimal(position.metadata_json["initial_position_risk"]) <= risk_budget
    assert position.metadata_json["paper_fill_source"] == "fresh_l2_vwap"
    assert Decimal(position.metadata_json["initial_risk_per_unit"]) == position.entry_price - Decimal("95")
    assert Decimal(position.metadata_json["highest_high_since_entry"]) == position.entry_price
    assert db.added_of(Order)[0].price == position.entry_price


def test_execute_paper_skips_standard_signal_without_fresh_book() -> None:
    engine = OrderExecutionEngine(Settings())
    db = FakePaperDb(book_enabled=False)

    assert engine.execute_paper(db, _paper_signal()) is None  # type: ignore[arg-type]
    assert db.added_of(Position) == []
    assert [row.event_type for row in db.added_of(DecisionLog)] == [
        "paper_order_skipped_missing_fresh_l2",
    ]


def test_execute_paper_skips_when_current_book_invalidates_original_plan() -> None:
    engine = OrderExecutionEngine(Settings())
    book = MarketL2Snapshot(
        market_id=1,
        bids=[{"px": "104", "sz": "1000"}],
        asks=[{"px": "106", "sz": "1000"}],
        updated_at=datetime.now(UTC),
    )
    db = FakePaperDb(book=book)

    assert engine.execute_paper(db, _paper_signal()) is None  # type: ignore[arg-type]
    assert db.added_of(Position) == []
    assert [row.event_type for row in db.added_of(DecisionLog)] == [
        "paper_order_skipped_stale_plan",
    ]


def test_external_paper_signal_keeps_existing_fill_path_without_book() -> None:
    engine = OrderExecutionEngine(Settings())
    signal = _paper_signal()
    signal.strategy_breakdown = {"_external_source": {"provider": "the_bull"}}
    db = FakePaperDb(book_enabled=False)

    position = engine.execute_paper(db, signal)  # type: ignore[arg-type]

    assert position is not None
    assert position.entry_price == signal.entry_price
    assert position.metadata_json["paper_fill_source"] == "external_signal_plan"


def test_paper_book_fill_rejects_stale_or_insufficient_depth() -> None:
    now = datetime(2026, 10, 29, 16, tzinfo=UTC)
    book = MarketL2Snapshot(
        market_id=1,
        bids=[{"px": "100", "sz": "1"}, {"px": "99", "sz": "2"}],
        asks=[{"px": "101", "sz": "1"}],
        updated_at=now,
    )
    db = FakePaperDb(book=book)

    assert _paper_l2_fill_price(
        db, 1, side="short", size=Decimal("2"), now=now,
    ) == Decimal("99.5")
    assert _paper_l2_fill_price(
        db, 1, side="long", size=Decimal("2"), now=now,
    ) is None
    assert _paper_l2_fill_price(
        db, 1, side="short", size=Decimal("2"), now=now + timedelta(seconds=16),
    ) is None


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
        opened_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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


def test_close_paper_positions_for_candle_moves_stop_to_breakeven_after_tp1() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=50,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_loss=Decimal("90"),
        status="open",
        opened_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        metadata_json={
            "source_timeframe": "5m",
            "initial_size": "1",
            "initial_stop_loss": "90",
            "initial_risk_per_unit": "10",
            "take_profit_plan": [
                {"level": "tp1", "price": "110", "allocation": "0.3", "filled": False},
                {"level": "tp2", "price": "120", "allocation": "0.3", "filled": False},
                {"level": "runner", "price": "", "allocation": "0.4", "filled": False, "runner": True},
            ],
        },
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("111"),
        low=Decimal("99"),
        close=Decimal("110"),
        volume=Decimal("1"),
    )
    market = Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC"},
    )
    db = FakeTimeStopDb(positions=[position])

    closed = engine.close_paper_positions_for_candle(db, market, candle)  # type: ignore[arg-type]

    assert closed == 0
    assert position.size == Decimal("0.7")
    assert position.metadata_json["tp1_hit"] is True
    assert position.stop_loss == Decimal("100")
    assert position.metadata_json["breakeven_triggered"] is True
    assert position.metadata_json["last_stop_update_reason"] == "breakeven_after_tp1"


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
        opened_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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
        open_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
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


def test_time_stop_closes_stale_unprofitable_paper_position_at_latest_candle_close() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=10,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m", "initial_size": "1"},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("98"),  # losing long — qualifies for time-stop
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "closeLosingPaperPositions": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 1, "live_alerted": 0}
    assert position.status == "closed"
    assert position.closed_at == candle_open + timedelta(minutes=5)
    assert position.realized_pnl == Decimal("-2")
    assert position.metadata_json["exit_reason"] == "time_stop"
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "paper_position_time_stop_closed"
        for row in db.added
    )


def test_time_stop_closes_losing_paper_position_by_default() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=12,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m", "initial_size": "1"},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("98"),
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 1, "live_alerted": 0}
    assert position.status == "closed"
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "paper_position_time_stop_closed"
        for row in db.added
    )


def test_time_stop_skips_profitable_paper_position() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=20,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m", "initial_size": "1"},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("100"),
        close=Decimal("104"),  # profitable long — time-stop must be skipped
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert position.status == "open"


def test_time_stop_skips_paper_position_with_tp1_hit() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=21,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m", "initial_size": "1", "tp1_hit": True},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("98"),  # currently losing, but TP1 was already taken
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert position.status == "open"


def test_time_stop_skips_paper_position_with_tp2_hit() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=22,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={
            "source_timeframe": "5m",
            "initial_size": "1",
            "tp1_hit": True,
            "tp2_hit": True,
            "runner_active": True,
        },
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("97"),
        close=Decimal("97"),
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert position.status == "open"


def test_time_stop_skips_live_alert_when_tp1_hit() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=23,
        market_id=1,
        mode="live",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=9),
        metadata_json={"source_timeframe": "5m", "tp1_hit": True},
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": False,
                "liveMode": "alert",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert "last_time_stop_alert_at" not in (position.metadata_json or {})
    assert not any(
        isinstance(row, DecisionLog) and row.event_type == "live_position_time_stop_alert"
        for row in db.added
    )


def test_time_stop_skips_live_alert_when_profitable() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    candle_open = datetime.now(UTC) - timedelta(minutes=5)
    position = Position(
        id=24,
        market_id=1,
        mode="live",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m"},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=candle_open,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("95"),
        close=Decimal("96"),  # profitable short — alert must be skipped
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": False,
                "liveMode": "alert",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert "last_time_stop_alert_at" not in (position.metadata_json or {})
    assert not any(
        isinstance(row, DecisionLog) and row.event_type == "live_position_time_stop_alert"
        for row in db.added
    )


def test_time_stop_keeps_recent_paper_position_open() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=11,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=2),
        metadata_json={"source_timeframe": "5m"},
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert position.status == "open"


def test_time_stop_does_not_close_without_candle_after_open() -> None:
    engine = OrderExecutionEngine(Settings())
    opened_at = datetime.now(UTC) - timedelta(hours=9)
    stale_candle_open = opened_at - timedelta(hours=1)
    position = Position(
        id=14,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=opened_at,
        metadata_json={"source_timeframe": "5m"},
    )
    candle = Candle(
        market_id=1,
        timeframe="5m",
        open_time=stale_candle_open,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[candle],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": True,
                "liveMode": "off",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert position.status == "open"
    assert position.closed_at is None


def test_stale_market_data_closes_paper_position_at_fresh_mark_without_candles() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=30,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("2"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=12),
        pnl_updated_at=datetime.now(UTC),
        metadata_json={
            "source_timeframe": "1h",
            "initial_size": "2",
            "last_mark_price": "96",
        },
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "staleMarketData": {
                "enabled": True,
                "paperMode": "close",
                "freshMarkMaxAgeMinutes": 5,
                "maxCandleAgeHoursByTimeframe": {"1h": 8},
            }
        },
    )

    result = engine.apply_stale_market_data_guard(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 1, "paper_alerted": 0}
    assert position.status == "closed"
    assert position.realized_pnl == Decimal("-8")
    assert position.metadata_json["exit_reason"] == "stale_market_data"
    assert any(
        isinstance(row, DecisionLog)
        and row.event_type == "paper_position_stale_market_data_closed"
        for row in db.added
    )


def test_stale_market_data_closes_spot_like_paper_position_with_old_candle() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=31,
        market_id=99,
        mode="paper",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(days=3),
        pnl_updated_at=datetime.now(UTC),
        metadata_json={
            "source_timeframe": "4h",
            "initial_size": "1",
            "last_mark_price": "90",
        },
    )
    stale_candle = Candle(
        market_id=99,
        timeframe="4h",
        open_time=datetime.now(UTC) - timedelta(days=2),
        open=Decimal("101"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[stale_candle],
        position_management={
            "staleMarketData": {
                "enabled": True,
                "paperMode": "close",
                "freshMarkMaxAgeMinutes": 5,
                "maxCandleAgeHoursByTimeframe": {"4h": 24},
            }
        },
    )

    result = engine.apply_stale_market_data_guard(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 1, "paper_alerted": 0}
    assert position.status == "closed"
    assert position.realized_pnl == Decimal("10")


def test_stale_market_data_alerts_but_does_not_close_without_fresh_mark() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=32,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=12),
        pnl_updated_at=datetime.now(UTC) - timedelta(hours=1),
        metadata_json={"source_timeframe": "1h", "last_mark_price": "98"},
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "staleMarketData": {
                "enabled": True,
                "paperMode": "close",
                "freshMarkMaxAgeMinutes": 5,
                "maxCandleAgeHoursByTimeframe": {"1h": 8},
            }
        },
    )

    result = engine.apply_stale_market_data_guard(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "paper_alerted": 1}
    assert position.status == "open"
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "paper_stale_market_data_alert"
        for row in db.added
    )


def test_stale_market_data_keeps_position_with_fresh_closed_candle() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=33,
        market_id=1,
        mode="paper",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=12),
        pnl_updated_at=datetime.now(UTC),
        metadata_json={"source_timeframe": "1h", "last_mark_price": "98"},
    )
    fresh_candle = Candle(
        market_id=1,
        timeframe="1h",
        open_time=datetime.now(UTC) - timedelta(hours=2),
        open=Decimal("99"),
        high=Decimal("100"),
        low=Decimal("97"),
        close=Decimal("98"),
        volume=Decimal("1"),
    )
    db = FakeTimeStopDb(
        positions=[position],
        candles=[fresh_candle],
        position_management={
            "staleMarketData": {
                "enabled": True,
                "paperMode": "close",
                "freshMarkMaxAgeMinutes": 5,
                "maxCandleAgeHoursByTimeframe": {"1h": 8},
            }
        },
    )

    result = engine.apply_stale_market_data_guard(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "paper_alerted": 0}
    assert position.status == "open"


def test_stale_market_data_guard_never_closes_live_position() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=34,
        market_id=1,
        mode="live",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(days=2),
        pnl_updated_at=datetime.now(UTC),
        metadata_json={"source_timeframe": "1h", "last_mark_price": "90"},
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "staleMarketData": {
                "enabled": True,
                "paperMode": "close",
                "freshMarkMaxAgeMinutes": 5,
                "maxCandleAgeHoursByTimeframe": {"1h": 8},
            }
        },
    )

    result = engine.apply_stale_market_data_guard(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "paper_alerted": 0}
    assert position.status == "open"


def test_time_stop_alerts_stale_live_position_without_closing() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=12,
        market_id=1,
        mode="live",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=9),
        metadata_json={"source_timeframe": "5m"},
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": False,
                "liveMode": "alert",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 1}
    assert position.status == "open"
    assert "last_time_stop_alert_at" in position.metadata_json
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "live_position_time_stop_alert"
        for row in db.added
    )


def test_time_stop_does_not_duplicate_recent_live_alert() -> None:
    engine = OrderExecutionEngine(Settings())
    position = Position(
        id=13,
        market_id=1,
        mode="live",
        side="short",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        opened_at=datetime.now(UTC) - timedelta(hours=9),
        metadata_json={
            "source_timeframe": "5m",
            "last_time_stop_alert_at": datetime.now(UTC).isoformat(),
        },
    )
    db = FakeTimeStopDb(
        positions=[position],
        position_management={
            "timeStop": {
                "enabled": True,
                "paperEnabled": False,
                "liveMode": "alert",
                "maxAgeHoursByTimeframe": {"5m": 8},
            }
        },
    )

    result = engine.apply_time_stops(db)  # type: ignore[arg-type]

    assert result == {"paper_closed": 0, "live_alerted": 0}
    assert not any(
        isinstance(row, DecisionLog) and row.event_type == "live_position_time_stop_alert"
        for row in db.added
    )


def _detail_position(*, side: str = "long", entry: str = "100") -> Position:
    return Position(
        id=99,
        market_id=1,
        mode="paper",
        side=side,
        entry_price=Decimal(entry),
        size=Decimal("1"),
        status="open",
        metadata_json={},
    )


def test_exit_reason_detail_keeps_initial_stop_label() -> None:
    engine = OrderExecutionEngine(Settings())
    position = _detail_position(side="long")
    metadata = {"initial_stop_loss": "95"}

    detail = engine._exit_reason_detail(position, metadata, Decimal("95"), "stop_loss")

    assert detail == "stop_loss"


def test_exit_reason_detail_labels_breakeven_stop() -> None:
    engine = OrderExecutionEngine(Settings())
    position = _detail_position(side="long")
    metadata = {"initial_stop_loss": "95"}

    detail = engine._exit_reason_detail(position, metadata, Decimal("100"), "stop_loss")

    assert detail == "breakeven_stop"


def test_exit_reason_detail_labels_trailing_stop_in_profit() -> None:
    engine = OrderExecutionEngine(Settings())
    long_position = _detail_position(side="long")
    short_position = _detail_position(side="short")
    metadata = {"initial_stop_loss": "95"}

    assert (
        engine._exit_reason_detail(long_position, metadata, Decimal("103"), "stop_loss")
        == "trailing_stop"
    )
    assert (
        engine._exit_reason_detail(
            short_position, {"initial_stop_loss": "105"}, Decimal("97"), "stop_loss"
        )
        == "trailing_stop"
    )


def test_exit_reason_detail_labels_tightened_stop_still_losing() -> None:
    engine = OrderExecutionEngine(Settings())
    position = _detail_position(side="long")
    metadata = {"initial_stop_loss": "95"}

    detail = engine._exit_reason_detail(position, metadata, Decimal("98"), "stop_loss")

    assert detail == "tightened_stop"


def test_exit_reason_detail_passes_through_non_stop_reasons() -> None:
    engine = OrderExecutionEngine(Settings())
    position = _detail_position(side="long")

    assert engine._exit_reason_detail(position, {}, Decimal("110"), "tp1") == "tp1"
    assert engine._exit_reason_detail(position, {}, Decimal("90"), "time_stop") == "time_stop"
