from datetime import UTC, datetime
from decimal import Decimal

from app.core.enums import OperationMode, SignalStatus
from app.db.models import AppSetting, DecisionLog, Market, Position, Signal
from app.worker import live_close_notifications, timeframe_schedule
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
        if model is AppSetting:
            # Nessun override runtime: usa i default del position management.
            return None
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


def _sync_market(market_id: int, symbol: str, market_type: str = "perp") -> Market:
    return Market(
        id=market_id,
        exchange="hyperliquid",
        symbol=symbol,
        base=symbol.split("/")[0],
        quote="USDC",
        market_type=market_type,
        active=True,
        metadata_json={},
    )


class SyncCursorFakeDb:
    def __init__(self) -> None:
        self.settings: dict[str, AppSetting] = {}

    def get(self, model: object, key: str) -> AppSetting | None:
        if model is AppSetting:
            return self.settings.get(key)
        raise AssertionError(f"unexpected get model: {model}")

    def add(self, row: object) -> None:
        if isinstance(row, AppSetting):
            self.settings[row.key] = row


def test_active_timeframes_use_boundary_window_without_history() -> None:
    timeframe_schedule._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["5m", "15m"], 15 * 60 * 1000 + 30_000) == [
        "5m",
        "15m",
    ]


def test_active_timeframes_catch_up_after_long_cycle() -> None:
    timeframe_schedule._LAST_TIMEFRAME_RUN_MS.clear()
    worker.mark_timeframes_run(["5m"], 0)

    assert worker.active_timeframes_for_tick(["5m"], 6 * 60 * 1000) == ["5m"]


def test_active_timeframes_do_not_run_unknown_timeframes() -> None:
    timeframe_schedule._LAST_TIMEFRAME_RUN_MS.clear()

    assert worker.active_timeframes_for_tick(["2m"], 0) == []


def test_next_daily_recap_timestamp_targets_10am_rome() -> None:
    before = datetime(2026, 6, 5, 7, 30, tzinfo=UTC)  # 09:30 Rome
    after = datetime(2026, 6, 5, 8, 30, tzinfo=UTC)   # 10:30 Rome

    next_before = datetime.fromtimestamp(
        worker.next_daily_recap_timestamp(before),
        tz=UTC,
    )
    next_after = datetime.fromtimestamp(
        worker.next_daily_recap_timestamp(after),
        tz=UTC,
    )

    assert next_before == datetime(2026, 6, 5, 8, 0, tzinfo=UTC)
    assert next_after == datetime(2026, 6, 6, 8, 0, tzinfo=UTC)


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

    # Una posizione 4h aggiunge anche il timeframe di riferimento del trailing (1h).
    assert worker.paper_management_timeframes(
        FakeDb(positions=positions, signals=signals),
        ["5m"],
    ) == ["5m", "1h", "4h"]


def test_open_position_trailing_market_ids_include_reference_timeframes() -> None:
    positions = [
        Position(
            market_id=19,
            mode="live",
            status="open",
            metadata_json={"source_timeframe": "15m"},
        ),
        Position(
            market_id=20,
            mode="paper",
            status="open",
            metadata_json={"source_signal_id": "42"},
        ),
        Position(
            market_id=21,
            mode="paper",
            status="open",
            metadata_json={"source_timeframe": "2m"},
        ),
    ]
    signals = {42: Signal(timeframe="4h")}

    assert worker.open_position_trailing_market_ids_by_timeframe(
        FakeDb(positions=positions, signals=signals)
    ) == {
        "5m": {19},
        "1h": {20},
    }


def test_worker_sync_pair_selection_uses_only_perps_and_prioritizes_open_or_recent(
    monkeypatch,
) -> None:
    db = SyncCursorFakeDb()
    markets = [
        _sync_market(1, "BTC"),
        _sync_market(2, "ETH"),
        _sync_market(3, "SOL"),
        _sync_market(4, "PURR/USDC", "spot"),
    ]
    monkeypatch.setattr(
        worker,
        "_open_position_market_ids",
        lambda _db, **kwargs: {3} if kwargs.get("mode") is None else set(),
    )
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: {2})

    pairs, stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=3,
        now=datetime(2026, 6, 16, tzinfo=UTC),
    )

    assert [(market.id, timeframe) for market, timeframe in pairs] == [
        (3, "5m"),
        (2, "5m"),
        (1, "5m"),
    ]
    assert stats["total_pairs"] == 3
    assert stats["selected_pairs"] == 3
    assert stats["priority_pairs"] == 2
    assert db.settings[worker.WORKER_MARKET_SYNC_CURSOR_KEY].value["universe_size"] == 1


def test_worker_sync_pair_selection_staggers_remaining_perps(monkeypatch) -> None:
    db = SyncCursorFakeDb()
    markets = [
        _sync_market(1, "BTC"),
        _sync_market(2, "ETH"),
        _sync_market(3, "SOL"),
    ]
    monkeypatch.setattr(worker, "_open_position_market_ids", lambda _db, **_kwargs: set())
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    first, first_stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=2,
        now=datetime(2026, 6, 16, tzinfo=UTC),
    )
    second, second_stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=2,
        now=datetime(2026, 6, 16, tzinfo=UTC),
    )

    assert [(market.id, timeframe) for market, timeframe in first] == [(1, "5m"), (2, "5m")]
    assert first_stats["next_cursor_offset"] == 2
    assert [(market.id, timeframe) for market, timeframe in second] == [(3, "5m"), (1, "5m")]
    assert second_stats["cursor_offset"] == 2
    assert second_stats["next_cursor_offset"] == 1


def test_worker_sync_pair_selection_does_not_starve_remaining_when_priorities_fill_budget(
    monkeypatch,
) -> None:
    db = SyncCursorFakeDb()
    markets = [
        _sync_market(1, "BTC"),
        _sync_market(2, "ETH"),
        _sync_market(3, "SOL"),
        _sync_market(4, "DOGE"),
        _sync_market(5, "XRP"),
    ]
    monkeypatch.setattr(
        worker,
        "_open_position_market_ids",
        lambda _db, **kwargs: {1, 2, 3} if kwargs.get("mode") is None else set(),
    )
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    first, first_stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["15m"],
        budget=2,
        now=datetime(2026, 6, 19, tzinfo=UTC),
    )
    second, second_stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["15m"],
        budget=2,
        now=datetime(2026, 6, 19, tzinfo=UTC),
    )

    assert [(market.id, timeframe) for market, timeframe in first] == [
        (1, "15m"),
        (4, "15m"),
    ]
    assert first_stats["priority_pairs"] == 1
    assert first_stats["next_cursor_offset"] == 1
    assert [(market.id, timeframe) for market, timeframe in second] == [
        (2, "15m"),
        (5, "15m"),
    ]
    assert second_stats["priority_pairs"] == 1
    assert second_stats["next_cursor_offset"] == 0


def test_worker_sync_pair_selection_preserves_cursor_when_budget_is_exhausted(
    monkeypatch,
) -> None:
    db = SyncCursorFakeDb()
    db.settings[worker.WORKER_MARKET_SYNC_CURSOR_KEY] = AppSetting(
        key=worker.WORKER_MARKET_SYNC_CURSOR_KEY,
        value={"offset": 7, "priority_offset": 3, "universe_size": 20},
    )
    monkeypatch.setattr(worker, "_open_position_market_ids", lambda _db, **_kwargs: set())
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    pairs, stats = worker.select_worker_sync_pairs(
        db,
        [_sync_market(1, "BTC")],
        ["15m"],
        budget=0,
        now=datetime(2026, 6, 19, tzinfo=UTC),
    )

    assert pairs == []
    assert stats["cursor_offset"] == 7
    assert stats["next_cursor_offset"] == 7
    assert db.settings[worker.WORKER_MARKET_SYNC_CURSOR_KEY].value["offset"] == 7
    assert db.settings[worker.WORKER_MARKET_SYNC_CURSOR_KEY].value["priority_offset"] == 3


def test_worker_sync_pair_selection_restricts_monitoring_only_market_scope(monkeypatch) -> None:
    db = SyncCursorFakeDb()
    markets = [
        _sync_market(1, "BTC"),
        _sync_market(2, "ETH"),
        _sync_market(3, "SOL"),
    ]
    monkeypatch.setattr(worker, "_open_position_market_ids", lambda _db, **_kwargs: set())
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    pairs, stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=10,
        now=datetime(2026, 6, 18, 12, 7, tzinfo=UTC),
        restricted_market_ids_by_timeframe={"5m": {1, 2}},
        priority_pair_keys={(2, "5m")},
    )

    assert [(market.id, timeframe) for market, timeframe in pairs] == [
        (2, "5m"),
        (1, "5m"),
    ]
    assert stats["total_pairs"] == 2


def test_monitoring_sync_scope_includes_open_position_trailing_markets(monkeypatch) -> None:
    db = SyncCursorFakeDb()
    markets = [
        _sync_market(1, "BTC"),
        _sync_market(2, "ETH"),
        _sync_market(3, "LINK"),
        _sync_market(4, "SOL"),
    ]
    restrictions, priority_pairs = worker._monitoring_sync_scope(
        markets,
        sync_active_timeframes=["15m", "5m"],
        monitoring_only_timeframes=["5m"],
        active_timeframes=["15m"],
        trailing_market_ids_by_timeframe={"5m": {3}},
    )
    monkeypatch.setattr(worker, "_open_position_market_ids", lambda _db, **_kwargs: set())
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    pairs, stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=10,
        now=datetime(2026, 6, 18, 12, 7, tzinfo=UTC),
        restricted_market_ids_by_timeframe=restrictions,
        priority_pair_keys=priority_pairs,
    )

    assert restrictions == {"5m": {1, 2, 3}}
    assert priority_pairs == {(1, "5m"), (2, "5m"), (3, "5m")}
    assert [(market.id, timeframe) for market, timeframe in pairs] == [
        (1, "5m"),
        (2, "5m"),
        (3, "5m"),
    ]
    assert stats["total_pairs"] == 3


def test_worker_sync_pair_selection_skips_current_closed_candles(monkeypatch) -> None:
    db = SyncCursorFakeDb()
    markets = [_sync_market(1, "BTC"), _sync_market(2, "ETH")]
    monkeypatch.setattr(worker, "_open_position_market_ids", lambda _db, **_kwargs: set())
    monkeypatch.setattr(worker, "_recent_signal_market_ids", lambda _db, _now: set())

    pairs, stats = worker.select_worker_sync_pairs(
        db,
        markets,
        ["5m"],
        budget=10,
        now=datetime(2026, 6, 18, 12, 7, tzinfo=UTC),
        latest_open_by_pair={
            (1, "5m"): datetime(2026, 6, 18, 12, 0, tzinfo=UTC),
            (2, "5m"): datetime(2026, 6, 18, 11, 55, tzinfo=UTC),
        },
    )

    assert [(market.id, timeframe) for market, timeframe in pairs] == [(2, "5m")]
    assert stats["total_pairs"] == 1


def test_display_live_close_reason_uses_trailing_stop_for_profitable_dynamic_stop() -> None:
    position = Position(
        side="long",
        entry_price=Decimal("100"),
        metadata_json={
            "trailing_stop_enabled": True,
            "last_stop_update_reason": "chandelier",
        },
    )

    assert live_close_notifications._display_live_close_reason(
        position,
        "stop_loss",
        exit_price=Decimal("108"),
        pnl_usd=Decimal("8"),
    ) == "trailing_stop"


def test_display_live_close_reason_keeps_stop_loss_without_profit() -> None:
    position = Position(
        side="long",
        entry_price=Decimal("100"),
        metadata_json={
            "trailing_stop_enabled": True,
            "last_stop_update_reason": "breakeven",
        },
    )

    assert live_close_notifications._display_live_close_reason(
        position,
        "stop_loss",
        exit_price=Decimal("95"),
        pnl_usd=Decimal("-5"),
    ) == "stop_loss"


def test_display_live_close_reason_uses_trailing_stop_for_profitable_close_without_metadata() -> None:
    position = Position(
        side="long",
        entry_price=Decimal("100"),
        metadata_json={},
    )

    assert live_close_notifications._display_live_close_reason(
        position,
        "stop_loss",
        exit_price=Decimal("108"),
        pnl_usd=Decimal("8"),
    ) == "trailing_stop"


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


def test_auto_trade_skips_live_when_paper_was_not_created() -> None:
    signal = _signal()
    db = FakeDb()
    executor = FakeExecutor(live_error=PermissionError("rejected"), paper_position=None)

    worker.execute_signal_for_mode(
        db, executor, signal, OperationMode.AUTO_TRADE, _market(), "5m"
    )

    assert executor.calls == ["paper"]
    assert signal.status == SignalStatus.IGNORED.value
    assert db.added[0] is signal
    assert isinstance(db.added[1], DecisionLog)
    assert db.added[1].event_type == "live_order_skipped_after_paper_skip"
    assert db.added[1].signal_id == signal.id
    assert db.committed == 1


def test_should_publish_public_signal_skips_ignored_signal() -> None:
    signal = _signal()
    signal.status = SignalStatus.IGNORED.value

    assert worker.should_publish_public_signal(signal, 70) is False


def test_should_publish_public_signal_keeps_executed_signal_above_threshold() -> None:
    signal = _signal()
    signal.status = SignalStatus.EXECUTED.value

    assert worker.should_publish_public_signal(signal, 70) is True
