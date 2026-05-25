from decimal import Decimal

from app.core.enums import OperationMode, SignalStatus
from app.db.models import Market, Position, Signal
from app.worker import main as worker


class FakeDb:
    def __init__(self, positions: list[Position] | None = None, signals: dict[int, Signal] | None = None) -> None:
        self.added: list[object] = []
        self.committed = 0
        self.positions = positions or []
        self.signals = signals or {}

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.committed += 1

    def query(self, model: object) -> "FakeQuery":
        if model is Position:
            return FakeQuery(self.positions)
        raise AssertionError(f"unexpected query model: {model}")

    def get(self, model: object, key: int) -> Signal | None:
        if model is Signal:
            return self.signals.get(key)
        raise AssertionError(f"unexpected get model: {model}")


class FakeQuery:
    def __init__(self, rows: list[Position]) -> None:
        self.rows = rows

    def filter(self, *args: object, **kwargs: object) -> "FakeQuery":
        return self

    def all(self) -> list[Position]:
        return list(self.rows)


class FakeExecutor:
    def __init__(self, live_error: Exception | None = None, paper_position: object | None = object()) -> None:
        self.live_error = live_error
        self.paper_position = paper_position
        self.calls: list[str] = []

    def execute_paper(self, db: FakeDb, signal: Signal) -> object | None:
        self.calls.append("paper")
        signal.status = SignalStatus.EXECUTED.value
        return self.paper_position

    def execute_live(self, db: FakeDb, signal: Signal) -> None:
        self.calls.append("live")
        if self.live_error is not None:
            raise self.live_error


def _signal() -> Signal:
    return Signal(
        id=42,
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
        status=SignalStatus.NEW.value,
    )


def _market() -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={},
    )


def test_active_timeframes_use_boundary_window_without_history() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["5m", "15m"], 15 * 60 * 1000 + 30_000) == [
        "5m",
        "15m",
    ]


def test_active_timeframes_catch_up_after_long_cycle() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()
    worker.mark_timeframes_run(["5m"], 0)

    assert worker.active_timeframes_for_tick(["5m"], 6 * 60 * 1000) == ["5m"]


def test_active_timeframes_do_not_run_unknown_timeframes() -> None:
    worker._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["2m"], 0) == []


def test_paper_management_timeframes_include_open_position_timeframes() -> None:
    positions = [
        Position(mode="paper", status="open", metadata_json={"source_timeframe": "30m"}),
        Position(mode="paper", status="open", metadata_json={"source_timeframe": "1h"}),
        Position(mode="paper", status="open", metadata_json={"source_timeframe": "2m"}),
        Position(mode="paper", status="open", metadata_json={"source_timeframe": "30m"}),
    ]

    assert worker.paper_management_timeframes(FakeDb(positions=positions), ["5m", "15m"]) == [
        "5m",
        "15m",
        "30m",
        "1h",
    ]


def test_paper_management_timeframes_fall_back_to_signal_timeframe() -> None:
    positions = [
        Position(mode="paper", status="open", metadata_json={"source_signal_id": "42"}),
    ]
    signals = {42: Signal(timeframe="4h")}

    assert worker.paper_management_timeframes(
        FakeDb(positions=positions, signals=signals),
        ["5m"],
    ) == ["5m", "4h"]


def test_auto_trade_executes_paper_before_live() -> None:
    signal = _signal()
    executor = FakeExecutor()

    worker.execute_signal_for_mode(
        FakeDb(), executor, signal, OperationMode.AUTO_TRADE, _market(), "5m"
    )

    assert executor.calls == ["paper", "live"]


def test_auto_trade_keeps_paper_when_live_is_rejected() -> None:
    signal = _signal()
    db = FakeDb()
    executor = FakeExecutor(live_error=PermissionError("rejected"))

    worker.execute_signal_for_mode(
        db, executor, signal, OperationMode.AUTO_TRADE, _market(), "5m"
    )

    assert executor.calls == ["paper", "live"]
    assert signal.status == SignalStatus.EXECUTED.value
    assert db.added == []
    assert db.committed == 0


def test_auto_trade_marks_signal_ignored_when_paper_was_not_created_and_live_is_rejected() -> None:
    signal = _signal()
    db = FakeDb()
    executor = FakeExecutor(live_error=PermissionError("rejected"), paper_position=None)

    worker.execute_signal_for_mode(
        db, executor, signal, OperationMode.AUTO_TRADE, _market(), "5m"
    )

    assert executor.calls == ["paper", "live"]
    assert signal.status == SignalStatus.IGNORED.value
    assert db.added == [signal]
    assert db.committed == 1
