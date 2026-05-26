from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.db.models import AppSetting, DecisionLog, Market, Order, Position, Signal
from app.engine.execution import OrderExecutionEngine, _target_leverage
from app.engine.order_validation import ValidationResult
from app.services.hyperliquid import HyperliquidError, HyperliquidOrderResult


@dataclass
class _FakeQuery:
    rows: list[Any]

    def filter(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[Any]:
        return list(self.rows)

    def count(self) -> int:
        return len(self.rows)


@dataclass
class FakeDb:
    """Minimal Session double for execute_live happy-path tests.

    Returns deterministic AppSettings, Market and Signal entries via `get()`,
    and empty `_FakeQuery` results for any unrelated query (so validation does
    not see duplicate positions / open positions / open risk).
    """

    signal: Signal
    market: Market
    risk_settings: dict[str, Any] = field(default_factory=dict)
    live_execution_settings: dict[str, Any] = field(default_factory=dict)
    operation_mode_setting: dict[str, Any] = field(default_factory=lambda: {"value": "AUTO_TRADE"})
    enable_live_setting: dict[str, Any] = field(default_factory=lambda: {"value": True})
    kill_switch: dict[str, Any] | None = None
    added: list[Any] = field(default_factory=list)
    committed: int = 0
    rolled_back: int = 0
    next_position_id: int = 1
    flushed: int = 0

    def get(self, model: type[Any], pk: Any) -> Any | None:
        if model is Signal and pk == self.signal.id:
            return self.signal
        if model is Market and pk == self.market.id:
            return self.market
        if model is AppSetting:
            if pk == "risk":
                return AppSetting(key="risk", value=self.risk_settings)
            if pk == "live_execution":
                return AppSetting(key="live_execution", value=self.live_execution_settings)
            if pk == "operation_mode":
                return AppSetting(key="operation_mode", value=self.operation_mode_setting)
            if pk == "enable_live_trading":
                return AppSetting(key="enable_live_trading", value=self.enable_live_setting)
            if pk == "kill_switch":
                return AppSetting(key="kill_switch", value=self.kill_switch) if self.kill_switch else None
        return None

    def query(self, *_args, **_kwargs) -> _FakeQuery:
        return _FakeQuery(rows=[])

    def add(self, row: Any) -> None:
        self.added.append(row)
        if isinstance(row, Position) and row.id is None:
            row.id = self.next_position_id
            self.next_position_id += 1

    def flush(self) -> None:
        self.flushed += 1
        for row in self.added:
            if isinstance(row, Position) and row.id is None:
                row.id = self.next_position_id
                self.next_position_id += 1

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def refresh(self, _row: Any) -> None:
        return None

    def added_of(self, model: type[Any]) -> list[Any]:
        return [row for row in self.added if isinstance(row, model)]


class FakeTradingClient:
    """Records every call made by execute_live so tests can assert on the
    submitted order parameters, and exposes hooks to simulate failures."""

    def __init__(
        self,
        account_value: Decimal = Decimal("1000"),
        market_status: str = "ok",
        trigger_failures: set[str] | None = None,
        user_state_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._account_value = account_value
        self.market_status = market_status
        self.trigger_failures = trigger_failures or set()
        self.user_state_positions = user_state_positions or []
        self.market_orders: list[dict[str, Any]] = []
        self.close_orders: list[dict[str, Any]] = []
        self.trigger_orders: list[dict[str, Any]] = []
        self.leverage_calls: list[dict[str, Any]] = []

    def account_value(self) -> Decimal:
        return self._account_value

    def set_leverage(self, coin: str, leverage: int, is_cross: bool = False) -> None:
        self.leverage_calls.append({"coin": coin, "leverage": leverage, "is_cross": is_cross})

    def submit_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        max_slippage_pct: float,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        self.market_orders.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "max_slippage_pct": max_slippage_pct,
                "expires_after_seconds": expires_after_seconds,
                "cloid": cloid,
            }
        )
        if self.market_status != "ok":
            return HyperliquidOrderResult(
                status=self.market_status,
                oid=None,
                avg_price=None,
                filled_size=None,
                raw_response={"status": self.market_status},
            )
        return HyperliquidOrderResult(
            status="ok",
            oid=f"mkt-{len(self.market_orders)}",
            avg_price=size and Decimal("100"),
            filled_size=size,
            raw_response={"status": "ok"},
        )

    def submit_trigger_order(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        expires_after_seconds: int,
        cloid: str | None = None,
    ) -> HyperliquidOrderResult:
        call = {
            "coin": coin,
            "is_buy": is_buy,
            "size": size,
            "trigger_price": trigger_price,
            "tpsl": tpsl,
            "expires_after_seconds": expires_after_seconds,
            "cloid": cloid,
        }
        self.trigger_orders.append(call)
        if tpsl in self.trigger_failures:
            raise HyperliquidError(f"simulated failure for {tpsl}")
        return HyperliquidOrderResult(
            status="ok",
            oid=f"trig-{tpsl}-{len(self.trigger_orders)}",
            avg_price=trigger_price,
            filled_size=size,
            raw_response={"status": "ok", "tpsl": tpsl},
        )

    def submit_market_close(
        self,
        coin: str,
        is_buy: bool,
        size: Decimal,
        max_slippage_pct: float,
        expires_after_seconds: int,
    ) -> HyperliquidOrderResult:
        self.close_orders.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "max_slippage_pct": max_slippage_pct,
                "expires_after_seconds": expires_after_seconds,
            }
        )
        return HyperliquidOrderResult(
            status="ok",
            oid=f"close-{len(self.close_orders)}",
            avg_price=Decimal("100"),
            filled_size=size,
            raw_response={"status": "ok"},
        )

    def user_state(self, _address: str | None = None) -> dict[str, Any]:
        return {"assetPositions": [{"position": row} for row in self.user_state_positions]}


def _build_settings() -> Settings:
    return Settings(
        operation_mode="AUTO_TRADE",
        enable_live_trading=True,
        hyperliquid_account_address="0xabc",
        hyperliquid_private_key="0xdef",
        hyperliquid_network="testnet",
        live_min_account_value_usd=10.0,
        live_order_notional_cap_usd=10_000.0,
        live_max_slippage_pct=0.5,
        live_order_expires_after_seconds=30,
        max_open_positions=5,
        max_total_exposure_pct=99.0,
        max_daily_loss_pct=99.0,
        max_leverage=10.0,
        max_risk_per_trade_pct=1.0,
    )


def _build_signal(direction: str = "LONG") -> Signal:
    return Signal(
        id=42,
        market_id=1,
        timeframe="5m",
        direction=direction,
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


def _build_short_signal() -> Signal:
    return Signal(
        id=43,
        market_id=1,
        timeframe="5m",
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


def _build_market() -> Market:
    return Market(
        id=1,
        exchange="hyperliquid",
        symbol="BTC",
        base="BTC",
        quote="USDC",
        market_type="perp",
        active=True,
        metadata_json={"hl_coin": "BTC", "sz_decimals": 4},
    )


def _force_validator_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.engine.order_validation import OrderValidator

    def fake_validate(self, db, signal, settings, plan=None):  # noqa: ARG001
        return ValidationResult(ok=True, reasons=[])

    monkeypatch.setattr(OrderValidator, "validate_live_execution", fake_validate)


def test_execute_live_long_submits_market_plus_dynamic_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    position = OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert position is not None
    assert position.side == "long"
    assert len(trading.market_orders) == 1
    assert trading.market_orders[0]["coin"] == "BTC"
    assert trading.market_orders[0]["is_buy"] is True
    assert len(trading.trigger_orders) == 3  # 1 SL + TP1/TP2; runner has no fixed TP
    tpsl_values = sorted(c["tpsl"] for c in trading.trigger_orders)
    assert tpsl_values == ["sl", "tp", "tp"]
    assert signal.status == "executed"
    orders = db.added_of(Order)
    # entry + SL + TP1/TP2
    assert len(orders) == 4


def test_execute_live_short_uses_sell_market_and_buy_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_short_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    position = OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert position.side == "short"
    assert trading.market_orders[0]["is_buy"] is False
    for trig in trading.trigger_orders:
        # short protection orders close the position → all must be buys
        assert trig["is_buy"] is True


def test_execute_live_raises_when_market_order_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient(market_status="err")

    with pytest.raises(HyperliquidError):
        OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    # No Position should be persisted when the market order was rejected
    assert db.added_of(Position) == []
    assert trading.trigger_orders == []


def test_execute_live_records_partial_failure_when_trigger_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient(trigger_failures={"sl"})

    position = OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert position is not None
    assert "protection_order_errors" in position.metadata_json
    assert any("stop_loss" in e for e in position.metadata_json["protection_order_errors"])
    events = [row.event_type for row in db.added_of(DecisionLog)]
    assert "live_protection_orders_partial_failure" in events


def test_execute_live_closes_position_when_stop_is_beyond_liquidation(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_short_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient(
        user_state_positions=[
            {
                "coin": "BTC",
                "szi": "-1",
                "liquidationPx": "104",
            }
        ]
    )

    with pytest.raises(PermissionError, match="stop loss is not before liquidation price"):
        OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert trading.close_orders != []
    assert trading.trigger_orders == []
    position = db.added_of(Position)[0]
    assert position.status == "closed"
    assert any(
        row.event_type == "live_position_liquidation_guard_failed"
        for row in db.added_of(DecisionLog)
    )


def test_execute_live_raises_permission_error_when_validation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.engine.order_validation import OrderValidator

    def fake_validate(self, db, signal, settings, plan=None):  # noqa: ARG001
        return ValidationResult(ok=False, reasons=["max open positions reached"])

    monkeypatch.setattr(OrderValidator, "validate_live_execution", fake_validate)

    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    with pytest.raises(PermissionError, match="max open positions reached"):
        OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert trading.market_orders == []
    assert db.added_of(Position) == []


def test_execute_live_sets_leverage_before_submitting_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert len(trading.leverage_calls) == 1
    assert trading.leverage_calls[0]["coin"] == "BTC"


def test_execute_live_caps_leverage_when_stop_is_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    signal.score = 74
    signal.entry_price = Decimal("2.4054")
    signal.stop_loss = Decimal("1.5835636328")
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert _target_leverage(signal, 10) == 2
    assert trading.leverage_calls[0]["leverage"] == 2


def test_execute_live_recovers_liquidation_guard_by_reducing_leverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    signal.score = 71
    signal.entry_price = Decimal("5.5108000000")
    signal.stop_loss = Decimal("4.6677655283")
    signal.take_profit_1 = Decimal("6.3538344717")
    signal.take_profit_2 = Decimal("7.1968689434")
    signal.take_profit_3 = Decimal("8.8039034050")

    class RecoveringTradingClient(FakeTradingClient):
        def submit_market_order(
            self,
            coin: str,
            is_buy: bool,
            size: Decimal,
            max_slippage_pct: float,
            expires_after_seconds: int,
            cloid: str | None = None,
        ) -> HyperliquidOrderResult:
            self.market_orders.append(
                {
                    "coin": coin,
                    "is_buy": is_buy,
                    "size": size,
                    "max_slippage_pct": max_slippage_pct,
                    "expires_after_seconds": expires_after_seconds,
                    "cloid": cloid,
                }
            )
            return HyperliquidOrderResult(
                status="ok",
                oid=f"mkt-{len(self.market_orders)}",
                avg_price=Decimal("5.4572000000"),
                filled_size=size,
                raw_response={"status": "ok"},
            )

        def user_state(self, _address: str | None = None) -> dict[str, Any]:
            liquidation_price = "4.8486638384" if len(self.leverage_calls) < 2 else "4.1000000000"
            return {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "1",
                            "liquidationPx": liquidation_price,
                        }
                    }
                ]
            }

    db = FakeDb(signal=signal, market=_build_market())
    trading = RecoveringTradingClient()

    position = OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert _target_leverage(signal, 10) == 3
    assert [call["leverage"] for call in trading.leverage_calls] == [3, 1]
    assert trading.close_orders == []
    assert len(trading.trigger_orders) == 3
    assert position.status == "open"
    assert position.metadata_json["contract_leverage"] == 1
    assert position.metadata_json["liquidation_guard_recovered"] is True
    events = [row.event_type for row in db.added_of(DecisionLog)]
    assert "live_position_liquidation_guard_failed" in events
    assert "live_liquidation_guard_recovered" in events


def test_execute_live_propagates_cloids_to_trading_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.engine.execution import make_cloid

    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    db = FakeDb(signal=signal, market=_build_market())
    trading = FakeTradingClient()

    OrderExecutionEngine(settings).execute_live(db, signal, trading_client=trading)

    assert trading.market_orders[0]["cloid"] == make_cloid(signal.id, "mkt")
    # all triggers got a non-empty cloid distinct from the market one
    cloids = [trig["cloid"] for trig in trading.trigger_orders]
    assert all(cloid is not None for cloid in cloids)
    assert len(set(cloids)) == len(cloids)
    assert trading.market_orders[0]["cloid"] not in cloids


def test_execute_live_is_idempotent_when_market_order_already_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry after a network blip must reuse the existing position instead of
    submitting a second market order."""
    from app.engine.execution import make_cloid

    _force_validator_ok(monkeypatch)
    settings = _build_settings()
    signal = _build_signal()
    market = _build_market()
    db = FakeDb(signal=signal, market=market)

    # Pretend a previous attempt already created a Position + market Order with the
    # deterministic cloid: the next execute_live must skip the market submit.
    pre_position = Position(
        id=99,
        market_id=market.id,
        mode="live",
        side="long",
        entry_price=Decimal("100"),
        size=Decimal("1"),
        status="open",
        metadata_json={},
    )
    pre_order = Order(
        position_id=99,
        signal_id=signal.id,
        cloid=make_cloid(signal.id, "mkt"),
        mode="live",
        order_type="market",
        side="long",
        price=Decimal("100"),
        size=Decimal("1"),
        status="filled",
        reduce_only=False,
        raw_response={},
    )
    db.added.extend([pre_order, pre_position])

    # Override the FakeDb to surface the prior records to the lookup queries.
    class IdempotentDb(FakeDb):
        def query(self, model: type[Any], *args: Any, **kwargs: Any) -> _FakeQuery:  # type: ignore[override]
            if model is Order:
                return _FakeQuery(rows=[pre_order])
            return _FakeQuery(rows=[])

        def get(self, model: type[Any], pk: Any) -> Any | None:  # type: ignore[override]
            if model is Position and pk == 99:
                return pre_position
            return super().get(model, pk)

    idem_db = IdempotentDb(signal=signal, market=market)
    trading = FakeTradingClient()

    returned = OrderExecutionEngine(settings).execute_live(idem_db, signal, trading_client=trading)

    assert returned is pre_position
    assert trading.market_orders == []
    assert trading.trigger_orders == []
