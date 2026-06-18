from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.db.models import (
    AppSetting,
    Candle,
    DecisionLog,
    Market,
    Order,
    Position,
    PositionStopUpdate,
    Signal,
)
from app.engine.execution import OrderExecutionEngine
from app.engine.position_management import (
    PositionManagementEngine,
    StopCandidate,
    r_multiple,
)
from app.services.hyperliquid import HyperliquidOrderResult


@dataclass
class _Query:
    rows: list[Any]

    def filter(self, *_args: Any, **_kwargs: Any) -> _Query:
        return self

    def order_by(self, *_args: Any, **_kwargs: Any) -> _Query:
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
    markets: dict[int, Market] = field(default_factory=dict)
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
        if model is Market:
            return self.markets.get(int(key))
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
    def __init__(
        self,
        submit_status: str = "ok",
        cancel_raises: bool = False,
        user_state_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.submit_status = submit_status
        self.cancel_raises = cancel_raises
        self.user_state_positions = user_state_positions or []
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

    def user_state(self, _address: str | None = None, *, max_age_seconds: float | None = None) -> dict[str, Any]:
        return {"assetPositions": [{"position": row} for row in self.user_state_positions]}


class FakeModifyTrading(FakeTrading):
    def __init__(self, *, raise_modify: Exception | None = None) -> None:
        super().__init__()
        self.raise_modify = raise_modify
        self.modified: list[dict[str, Any]] = []

    def modify_trigger_order(
        self,
        *,
        oid: int,
        coin: str,
        is_buy: bool,
        size: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        self.modified.append(
            {
                "oid": oid,
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "trigger_price": trigger_price,
                "tpsl": tpsl,
                "expires_after_seconds": expires_after_seconds,
                "cloid": cloid,
            }
        )
        if self.raise_modify is not None:
            raise self.raise_modify
        return HyperliquidOrderResult(
            status="ok",
            oid=str(oid),
            avg_price=None,
            filled_size=None,
            raw_response={"status": "ok", "modified": True},
        )


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
        opened_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        metadata_json={
            "source_timeframe": "5m",
            "initial_size": "10",
            "initial_stop_loss": stop,
            "initial_risk_per_unit": "10",
        },
    )


def test_take_profit_plan_skips_non_positive_short_targets() -> None:
    signal = Signal(
        id=1,
        market_id=1,
        timeframe="5m",
        direction="SHORT",
        score=80,
        confidence_label="high",
        entry_price=Decimal("0.10"),
        stop_loss=Decimal("0.18"),
        reason="test",
        strategy_breakdown={},
        status="executed",
    )
    db = FakeDb(
        settings={
            "position_management": AppSetting(
                key="position_management",
                value={
                    "takeProfit": {
                        "enabled": True,
                        "tp1R": 1.0,
                        "tp1ClosePct": 30,
                        "tp2R": 2.0,
                        "tp2ClosePct": 30,
                        "runnerPct": 40,
                    }
                },
            )
        }
    )

    plan = PositionManagementEngine(Settings()).build_take_profit_plan(db, signal)  # type: ignore[arg-type]

    assert [item["level"] for item in plan] == ["tp1", "runner"]
    assert Decimal(str(plan[0]["price"])) > 0


def _candles(close: str = "112", count: int = 30) -> list[Candle]:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
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
    return FakeDb(settings=settings, markets={1: _market()})


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


def test_trailing_uses_reference_timeframe_for_slow_positions() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    # Posizione short su timeframe 1d: il trailing deve essere valutato su 1h.
    position = _position("short", "110")
    position.metadata_json["source_timeframe"] = "1d"

    assert engine.trailing_timeframe(db, position) == "1h"

    # Serie giornaliera in discesa (short in profitto): usata per ATR/EMA.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    daily: list[Candle] = []
    for idx in range(30):
        base = Decimal("100") - Decimal(idx) * Decimal("0.3")
        close = base - Decimal("0.5")
        daily.append(
            Candle(
                market_id=1,
                timeframe="1d",
                open_time=start + timedelta(days=idx),
                open=base,
                high=base + Decimal("1.5"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("1"),
            )
        )
    db.candles = daily

    # Candela 1h di riferimento con un nuovo minimo intraday.
    reference = Candle(
        market_id=1,
        timeframe="1h",
        open_time=start + timedelta(days=30),
        open=Decimal("88"),
        high=Decimal("88.5"),
        low=Decimal("86"),
        close=Decimal("87"),
        volume=Decimal("1"),
    )

    candidate = engine.evaluate_stop_candidate(db, _market(), position, [reference], reference)

    assert candidate is not None
    # Lo stop dello short si stringe verso il basso, sotto lo stop attuale (110).
    assert candidate.price < Decimal("110")
    # ATR/EMA calcolati sulle candele 1d (larghezza ampia), non sulla candela 1h.
    assert Decimal(candidate.indicators["atr"]) > Decimal("0")
    # Il gate del trailing usa la candela di riferimento (1h).
    assert position.metadata_json["last_trailing_candle_open_time"] == reference.open_time.isoformat()


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


def test_apply_stop_update_falls_back_when_current_price_indicator_is_none() -> None:
    engine = PositionManagementEngine(Settings())
    db = _db()
    position = _position("long", "90")

    engine.apply_stop_update(
        db,
        position,
        StopCandidate(
            price=Decimal("100"),
            reason="breakeven_after_tp1",
            indicators={"current_price": None},
            r_multiple=Decimal("1"),
        ),
        mode="paper",
    )

    updates = [row for row in db.added if isinstance(row, PositionStopUpdate)]
    assert updates
    assert updates[-1].current_price == Decimal("100")


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


def test_live_replacement_modifies_existing_stop_when_supported() -> None:
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
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )
    trading = FakeModifyTrading()

    assert engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert trading.modified[0]["oid"] == 111
    assert trading.submitted == []
    assert trading.canceled == []
    assert old_order.price == Decimal("95")
    assert position.stop_loss == Decimal("95")
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "live_stop_update_modify_succeeded"
        for row in db.added
    )


def test_live_stop_modify_429_records_pending_without_mutating_stop() -> None:
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
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )
    trading = FakeModifyTrading(
        raise_modify=RuntimeError(
            "Hyperliquid rate-limit governor skipped exchange:modify; "
            "reason=address_action_spacing; next_retry_after=2026-06-17T12:00:10+00:00"
        )
    )

    assert not engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert position.stop_loss == Decimal("90")
    assert old_order.price == Decimal("90")
    assert trading.submitted == []
    pending = [
        row for row in db.added
        if isinstance(row, DecisionLog) and row.event_type == "live_stop_update_pending_retry"
    ]
    assert pending
    assert pending[-1].data["submit_attempts"] == 0
    assert pending[-1].data["rate_limit_class"] == "exchange"
    assert pending[-1].data["next_retry_after"] == "2026-06-17T12:00:10+00:00"


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


def test_liquidation_guard_rejects_live_stop_update_beyond_liq() -> None:
    engine = PositionManagementEngine(Settings(hyperliquid_account_address="0xabc"))
    db = _db()
    position = _position("long", "90", mode="live")
    trading = FakeTrading(
        user_state_positions=[
            {
                "coin": "BTC",
                "szi": "10",
                "liquidationPx": "101",
            }
        ]
    )
    candidate = StopCandidate(
        price=Decimal("100"),
        reason="breakeven",
        indicators={"current_price": "112"},
        r_multiple=Decimal("1.2"),
    )

    assert not engine.replace_live_stop_order(db, _market(), position, candidate, trading)
    assert trading.submitted == []
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "live_position_liquidation_guard_failed"
        for row in db.added
    )


def test_periodic_liquidation_guard_audit_records_failures() -> None:
    engine = PositionManagementEngine(Settings(hyperliquid_account_address="0xabc"))
    db = _db()
    long_position = _position("long", "90", mode="live")
    short_position = _position("short", "105", mode="live")
    short_position.id = 11
    short_position.market = _market()
    long_position.market = _market()
    db.positions.extend([long_position, short_position])
    trading = FakeTrading(
        user_state_positions=[
            {
                "coin": "BTC",
                "szi": "10",
                "liquidationPx": "80",
            }
        ]
    )

    result = engine.audit_live_positions_against_liquidation(db, trading_client=trading)

    assert result == {"checked": 2, "failed": 1, "skipped": 0}
    assert long_position.metadata_json["last_liquidation_guard_status"] == "ok"
    assert short_position.metadata_json["last_liquidation_guard_status"] == "failed"
    assert any(
        isinstance(row, DecisionLog) and row.event_type == "live_liquidation_guard_cycle"
        for row in db.added
    )


# ---------------------------------------------------------------------------
# Tests for replace_live_stop_order retry logic
# ---------------------------------------------------------------------------

class _RetryTrading:
    """Simula N fallimenti consecutivi, poi successo."""

    def __init__(self, fail_times: int = 0, raise_exc: bool = False) -> None:
        self.fail_times = fail_times
        self.raise_exc = raise_exc
        self.call_count = 0
        self.submitted: list[dict] = []
        self.canceled: list[dict] = []

    def submit_trigger_order(self, *, coin, is_buy, size, trigger_price, tpsl, expires_after_seconds, cloid=None):
        self.call_count += 1
        self.submitted.append({"cloid": cloid, "trigger_price": trigger_price})
        if self.call_count <= self.fail_times:
            if self.raise_exc:
                raise RuntimeError("network error")
            return HyperliquidOrderResult(status="error", oid=None, avg_price=None, filled_size=None, raw_response={"error": "rejected"})
        return HyperliquidOrderResult(status="ok", oid="999", avg_price=None, filled_size=None, raw_response={"status": "ok"})

    def cancel_order(self, coin, oid, expires_after_seconds):
        self.canceled.append({"coin": coin, "oid": oid})
        return {"status": "ok"}

    def user_state(self, _address=None, *, max_age_seconds=None):
        return {"assetPositions": []}


def _live_position_with_metadata() -> Position:
    p = _position(side="long", stop="90", mode="live")
    p.metadata_json = {
        "source_timeframe": "5m",
        "initial_size": "10",
        "initial_stop_loss": "90",
        "initial_risk_per_unit": "10",
    }
    return p


def test_submit_failure_records_pending_without_retry() -> None:
    engine = PositionManagementEngine(Settings())
    position = _live_position_with_metadata()
    db = _db()
    trading = _RetryTrading(fail_times=1)
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )

    result = engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert result is False
    assert trading.call_count == 1
    failure_logs = [
        r for r in db.added
        if isinstance(r, DecisionLog) and r.event_type == "live_stop_update_submit_failed"
    ]
    assert len(failure_logs) == 1
    assert failure_logs[0].data["submit_attempts"] == 1


def test_successful_stop_replacement_clears_previous_failure() -> None:
    engine = PositionManagementEngine(Settings())
    position = _live_position_with_metadata()
    position.metadata_json["last_stop_replacement_failure"] = {"reason": "submit_rejected"}
    position.metadata_json["stop_replacement_attempts"] = 2
    db = _db()
    trading = _RetryTrading(fail_times=0)
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )

    result = engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert result is True
    assert position.metadata_json["stop_replacement_attempts"] == 0
    assert "last_stop_replacement_failure" not in position.metadata_json


def test_retry_all_attempts_fail_records_failure_once() -> None:
    engine = PositionManagementEngine(Settings())
    position = _live_position_with_metadata()
    db = _db()
    trading = _RetryTrading(fail_times=99)
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )

    result = engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert result is False
    assert trading.call_count == 1
    failure_logs = [
        r for r in db.added
        if isinstance(r, DecisionLog) and r.event_type == "live_stop_update_submit_failed"
    ]
    assert len(failure_logs) == 1  # un solo log, non quattro
    assert failure_logs[0].data.get("safety_critical") is True  # candidate reason="breakeven"


def test_submit_exception_records_failure_without_retry() -> None:
    engine = PositionManagementEngine(Settings())
    position = _live_position_with_metadata()
    db = _db()
    trading = _RetryTrading(fail_times=2, raise_exc=True)
    candidate = StopCandidate(
        price=Decimal("95"),
        reason="breakeven",
        indicators={"current_price": "105"},
        r_multiple=Decimal("1"),
    )

    result = engine.replace_live_stop_order(db, _market(), position, candidate, trading)

    assert result is False
    assert trading.call_count == 1
    failure_logs = [
        r for r in db.added
        if isinstance(r, DecisionLog) and r.event_type == "live_stop_update_submit_failed"
    ]
    assert len(failure_logs) == 1
    assert failure_logs[0].data["submit_attempts"] == 1
