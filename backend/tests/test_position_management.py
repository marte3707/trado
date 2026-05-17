from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import AppSetting, Candle, DecisionLog, Market, Order, Position, PositionStopUpdate, Signal
from app.engine.execution import OrderExecutionEngine
from app.engine.position_management import PositionManagementEngine, r_multiple
from app.services.hyperliquid import HyperliquidOrderResult


@dataclass
class _Query:
    rows: list[Any]

    def filter(self, *_args: Any, **_kwargs: Any) -> "_Query":
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> "_Query":
        return self

    def all(self) -> list[Any]:
        return list(self.rows)

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None


@dataclass
class FakeDb:
    settings: dict[str, AppSetting] = field(default_factory=dict)
    signals: dict[int, Signal] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    candles: list[Candle] = field(default_factory=list)
    added: list[Any] = field(default_factory=list)
    committed: int = 0
    flushed: int = 0
    next_order_id: int = 100

    def get(self, model: type[Any], key: Any) -> Any | None:
        if model is AppSetting:
            return self.settings.get(str(key))
        if model is Signal:
            return self.signals.get(int(key))
        return None

    def query(self, model: type[Any]) -> _Query:
        if model is Order:
            return _Query(self.orders)
        if model is Position:
            return _Query(self.positions)
        if model is Candle:
            return _Query(self.candles)
        return _Query([])

    def add(self, row: Any) -> None:
        if isinstance(row, Order) and row.id is None:
            row.id = self.next_order_id
            self.next_order_id += 1
        self.added.append(row)

    def flush(self) -> None:
        self.flushed += 1
        for row in self.added:
            if isinstance(row, Order) and row.id is None:
                row.id = self.next_order_id
                self.next_order_id += 1

    def commit(self) -> None:
        self.committed += 1


class FakeTrading:
    def __init__(self, submit_status: str = "ok", cancel_raises: bool = False) -> None:
        self.submit_status = submit_status
        self.cancel_raises = cancel_raises
        self.submitted: list[dict[str, Any]] = []
        self.canceled: list[dict[str, Any]] = []

    def submit_trigger_order(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        self.submitted.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "trigger_price": trigger_price,
                "tpsl": tpsl,
                "expires_after_seconds": expires_after_seconds,
                "cloid": cloid,
            }
        )
        return HyperliquidOrderResult(
            status=self.submit_status,
            oid="222",
            avg_price=None,
            filled_size=None,
            raw_response={"status": self.submit_status},
        )

    def cancel_order(self, coin: str, oid: int, expires_after_seconds: int) -> dict[str, Any]:
        if self.cancel_raises:
            raise RuntimeError("cancel failed")
        self.canceled.append({"coin": coin, "oid": oid, "expires_after_seconds": expires_after_seconds})
        return {"status": "ok"}


def _market(active: bool = True) -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=active,
        metadata_json={"hl_coin": "BTC", "raw": {"szDecimals": 4}},
    )


def _position(side: str = "long", stop: str = "90", mode: str = "paper") -> Position:
    return Position(
        id=10,
        market_id=1,
        mode=mode,
        side=side,
        entry_price=Decimal("100"),
        size=Decimal("10"),
        stop_loss=Decimal(stop),
        status="open",
        opened_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        metadata_json={
            "source_timeframe": "5m",
            "initial_size": "10",
            "initial_stop_loss": stop,
            "initial_risk_per_unit": "10",
        },
    )


def _candles(close: str = "112", count: int = 30) -> list[Candle]:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows: list[Candle] = []
    for idx in range(count):
        base = Decimal("100") + Decimal(idx) * Decimal("0.3")
        final_close = Decimal(close) if idx == count - 1 else base + Decimal("0.5")
        rows.append(
            Candle(
                market_id=1,
                timeframe="5m",
                open_time=start + timedelta(minutes=5 * idx),
                open=base,
                high=max(base + Decimal("1.5"), final_close + Decimal("1")),
                low=min(base - Decimal("1.5"), final_close - Decimal("1")),
                close=final_close,
                volume=Decimal("1"),
            )
        )
    return rows


def _db(config: dict[str, Any] | None = None, kill: bool = False) -> FakeDb:
    settings = {
        "kill_switch": AppSetting(key="kill_switch", value={"enabled": kill}),
        "operation_mode": AppSetting(
            key="operation_mode", value={"mode": "AUTO_TRADE", "enable_live_trading": True}
        ),
        "live_execution": AppSetting(key="live_execution", value={}),
    }
    if config is not None:
        settings["position_management"] = AppSetting(key="position_management", value=config)
    return FakeDb(settings=settings)


def test_r_multiple_long_and_short() -> None:
    long = _position("long", "90")
    short = _position("short", "110")

    assert r_multiple(long, Decimal("115"), Decimal("10")) == Decimal("1.5")
    assert r_multiple(short, Decimal("85"), Decimal("10")) == Decimal("1.5")


def test_trailing_long_moves_stop_up_and_records_history() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "90")
    candles = _candles("112")

    assert engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])

    assert position.stop_loss is not None
    assert position.stop_loss >= Decimal("100")
    assert position.metadata_json["breakeven_triggered"] is True
    assert any(isinstance(row, PositionStopUpdate) for row in db.added)
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "position_stop_updated"
        for row in db.added
    )


def test_trailing_short_moves_stop_down() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("short", "110")
    candles = _candles("88")

    assert engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])

    assert position.stop_loss is not None
    assert position.stop_loss <= Decimal("100")


def test_stop_never_moves_against_position() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "105")
    candles = _candles("112")

    assert not engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])
    assert position.stop_loss == Decimal("105")


def test_lock_profit_can_move_stop_past_breakeven() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "90")
    candles = _candles("123")

    assert engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])

    assert position.stop_loss is not None
    assert position.stop_loss >= Decimal("110")


def test_no_update_below_min_distance() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db({"trailingStop": {"minStopUpdateDistancePct": 50}})
    position = _position("long", "100")
    candles = _candles("123")

    assert not engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])
    assert position.stop_loss == Decimal("100")


def test_missing_indicator_data_skips_update() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "90")
    candles = _candles("112", count=5)

    assert not engine.manage_paper_position_for_candle(db, _market(), position, candles, candles[-1])


def test_kill_switch_or_inactive_market_skips_update() -> None:
    engine = PositionManagementEngine(Settings())
    position = _position("long", "90")
    candles = _candles("112")

    assert not engine.manage_paper_position_for_candle(
        _db(kill=True), _market(), position, candles, candles[-1]
    )
    assert not engine.manage_paper_position_for_candle(
        _db(), _market(active=False), position, candles, candles[-1]
    )


def test_tp1_tp2_runner_and_no_double_trigger() -> None:
    signal = Signal(
        id=1,
        market_id=1,
        timeframe="5m",
        direction="LONG",
        score=80,
        confidence_label="high",
        entry_price=Decimal("100"),
        stop_loss=Decimal("90"),
        reason="test",
        strategy_breakdown={},
    )
    db = _db()
    db.signals[1] = signal
    executor = OrderExecutionEngine(Settings())
    position = _position("long", "90")
    position.metadata_json = {
        "source_signal_id": "1",
        "source_timeframe": "5m",
        "initial_size": "10",
        "take_profit_plan": executor._build_take_profit_plan(db, signal),  # type: ignore[arg-type]
    }
    first = _candles("111")[-1]
    second = _candles("121")[-1]

    assert executor._apply_paper_triggers_for_candle(db, position, first) == (1, 0)  # type: ignore[arg-type]
    assert position.size == Decimal("7.0")
    assert position.metadata_json["tp1_hit"] is True

    assert executor._apply_paper_triggers_for_candle(db, position, first) == (0, 0)  # type: ignore[arg-type]
    assert position.size == Decimal("7.0")

    assert executor._apply_paper_triggers_for_candle(db, position, second) == (1, 0)  # type: ignore[arg-type]
    assert position.status == "open"
    assert position.size == Decimal("4.0")
    assert position.metadata_json["tp2_hit"] is True
    assert position.metadata_json["runner_active"] is True


def test_live_replacement_submits_new_stop_before_canceling_old_stop() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "90", mode="live")
    old_order = Order(
        id=55,
        position_id=10,
        hyperliquid_order_id="111",
        cloid="old",
        mode="live",
        order_type="stop_loss",
        side="sell",
        price=Decimal("90"),
        size=Decimal("10"),
        status="ok",
        reduce_only=True,
        raw_response={},
    )
    db.orders.append(old_order)
    candles = _candles("112")
    candidate = engine.evaluate_stop_candidate(db, _market(), position, candles, candles[-1])
    assert candidate is not None
    trading = FakeTrading()

    assert engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert trading.submitted[0]["trigger_price"] == position.stop_loss
    assert trading.canceled[0]["oid"] == 111
    assert old_order.status == "canceled"
    assert any(isinstance(row, Order) and row.raw_response.get("dynamic_stop_update") for row in db.added)


def test_signal_only_mode_blocks_live_replacement() -> None:
    engine = PositionManagementEngine(Settings(operation_mode="SIGNAL_ONLY", enable_live_trading=False))
    db = _db()
    db.settings["operation_mode"] = AppSetting(
        key="operation_mode", value={"mode": "SIGNAL_ONLY", "enable_live_trading": False}
    )
    position = _position("long", "90", mode="live")
    candles = _candles("112")
    candidate = engine.evaluate_stop_candidate(db, _market(), position, candles, candles[-1])
    assert candidate is not None

    assert not engine.replace_live_stop_order(db, _market(), position, candidate, FakeTrading())
