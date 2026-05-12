from decimal import Decimal

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import Signal
from app.engine.order_validation import OrderValidator


class FakeQuery:
    def __init__(self, count_val: int = 0) -> None:
        self._count = count_val

    def filter(self, *_args):
        return self

    def count(self):
        return self._count


class FakeDb:
    def __init__(self, open_positions: int = 0) -> None:
        self._open_positions = open_positions

    def get(self, *_args):
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
    )
    result = OrderValidator().validate_live_execution(FakeDb(open_positions=0), _make_signal(), settings)  # type: ignore[arg-type]
    assert result.ok

