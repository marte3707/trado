from decimal import Decimal

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting, Market, Signal
from app.engine.order_validation import LiveOrderPlan, OrderValidator


class FakeQuery:
    def __init__(self, count_val: int = 0) -> None:
        self._count = count_val

    def filter(self, *_args):
        return self

    def count(self):
        return self._count

    def first(self):
        return None

    def all(self):
        return []

    def scalar(self):
        return 0


class FakeDb:
    def __init__(self, open_positions: int = 0) -> None:
        self._open_positions = open_positions
        self.market = Market(id=1, exchange="hyperliquid", symbol="BTC", base="BTC", quote="USDC", market_type="perp")

    def get(self, model, key):
        if model is Market and key == 1:
            return self.market
        if model is AppSetting:
            return None
        return None

    def query(self, *_args):
        return FakeQuery(self._open_positions)


def _make_signal() -> Signal:
    return Signal(
        market_id=1,
        timeframe="5m",
        direction="LONG",
        score=90,
        confidence_label="forte",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit_1=Decimal("105"),
        take_profit_2=Decimal("110"),
        risk_reward=Decimal("2"),
        reason="test",
        strategy_breakdown={},
        status="new",
    )


def test_live_validation_rejects_default_non_live_mode() -> None:
    settings = Settings(operation_mode=OperationMode.SIGNAL_ONLY, enable_live_trading=False)
    result = OrderValidator().validate_live_execution(FakeDb(), _make_signal(), settings)  # type: ignore[arg-type]
    assert not result.ok
    assert "operation mode is not AUTO_TRADE" in result.reasons


def test_live_validation_blocks_when_max_live_positions_reached() -> None:
    settings = Settings(
        operation_mode=OperationMode.AUTO_TRADE,
        enable_live_trading=True,
        max_open_positions=3,
        hyperliquid_network="testnet",
        hyperliquid_account_address="0x0000000000000000000000000000000000000001",
        hyperliquid_private_key="0x" + "1" * 64,
    )
    # FakeDb returns 5 open live positions — over the limit of 3
    result = OrderValidator().validate_live_execution(FakeDb(open_positions=5), _make_signal(), settings)  # type: ignore[arg-type]
    assert not result.ok
    assert "max open positions reached" in result.reasons


def test_live_validation_passes_when_no_live_positions_open() -> None:
    # In paper/signal mode can accumulate many trades; live limit must only count live positions.
    # FakeDb returns 0 live positions → limit not reached even if many paper ones exist.
    settings = Settings(
        operation_mode=OperationMode.AUTO_TRADE,
        enable_live_trading=True,
        max_open_positions=3,
        hyperliquid_network="testnet",
        hyperliquid_account_address="0x0000000000000000000000000000000000000001",
        hyperliquid_private_key="0x" + "1" * 64,
    )
    plan = LiveOrderPlan(
        side="long",
        size=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("0.1"),
        account_value=Decimal("1000"),
        risk_amount=Decimal("5"),
    )
    result = OrderValidator().validate_live_execution(FakeDb(open_positions=0), _make_signal(), settings, plan)  # type: ignore[arg-type]
    assert result.ok


def test_live_validation_blocks_when_notional_below_exchange_minimum() -> None:
    settings = Settings(
        operation_mode=OperationMode.AUTO_TRADE,
        enable_live_trading=True,
        max_open_positions=3,
        hyperliquid_network="testnet",
        hyperliquid_account_address="0x0000000000000000000000000000000000000001",
        hyperliquid_private_key="0x" + "1" * 64,
        live_min_account_value_usd=10.0,
        live_min_order_notional_usd=10.0,
    )
    # plan notional ($5) is below the $10 Hyperliquid minimum → must be rejected
    # pre-trade rather than being passed to the exchange.
    plan = LiveOrderPlan(
        side="long",
        size=Decimal("0.05"),
        notional=Decimal("5"),
        leverage=Decimal("0.005"),
        account_value=Decimal("1000"),
        risk_amount=Decimal("0.25"),
    )
    result = OrderValidator().validate_live_execution(FakeDb(open_positions=0), _make_signal(), settings, plan)  # type: ignore[arg-type]
    assert not result.ok
    assert any("notional below exchange minimum" in r for r in result.reasons)


def test_live_validation_blocks_spot_markets() -> None:
    settings = Settings(
        operation_mode=OperationMode.AUTO_TRADE,
        enable_live_trading=True,
        hyperliquid_network="testnet",
        hyperliquid_account_address="0x0000000000000000000000000000000000000001",
        hyperliquid_private_key="0x" + "1" * 64,
    )
    db = FakeDb(open_positions=0)
    db.market.market_type = "spot"

    result = OrderValidator().validate_live_execution(db, _make_signal(), settings)  # type: ignore[arg-type]

    assert not result.ok
    assert "live execution currently supports perp markets only" in result.reasons
