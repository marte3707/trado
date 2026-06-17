from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.models import Market
from app.services.candle_sync import TIMEFRAME_MS, CandleSyncService


@dataclass
class _ScalarQuery:
    value: Any | None

    def filter(self, *_args, **_kwargs) -> "_ScalarQuery":
        return self

    def order_by(self, *_args, **_kwargs) -> "_ScalarQuery":
        return self

    def limit(self, _n: int) -> "_ScalarQuery":
        return self

    def scalar(self) -> Any | None:
        return self.value


@dataclass
class FakeDb:
    latest_open: datetime | None
    executed: list[Any] = field(default_factory=list)
    committed: int = 0

    def query(self, *_args, **_kwargs) -> _ScalarQuery:
        return _ScalarQuery(value=self.latest_open)

    def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)

    def commit(self) -> None:
        self.committed += 1


class _SpyClient:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.captured: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.rows = rows or []

    async def candles(
        self,
        coin: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        attempts: int = 2,
    ) -> list[dict[str, Any]]:
        self.captured = {
            "coin": coin,
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "attempts": attempts,
        }
        self.calls.append(self.captured)
        return self.rows


def _market(*, market_id: int = 1, market_type: str = "perp", symbol: str = "BTC") -> Market:
    return Market(
        id=market_id,
        exchange="hyperliquid",
        symbol=symbol,
        base=symbol.split("/")[0],
        quote="USDC",
        market_type=market_type,
        active=True,
        metadata_json={"hl_coin": symbol},
    )


@pytest.mark.asyncio
async def test_sync_uses_full_lookback_when_table_is_empty() -> None:
    db = FakeDb(latest_open=None)
    service = CandleSyncService()
    spy = _SpyClient()
    service.client = spy  # type: ignore[assignment]

    await service.sync_market(db, _market(), "5m", lookback=300)

    interval_ms = TIMEFRAME_MS["5m"]
    requested_window_ms = spy.captured["end_ms"] - spy.captured["start_ms"]
    # Empty DB → request ~ lookback * interval_ms back
    assert requested_window_ms == interval_ms * 300


@pytest.mark.asyncio
async def test_sync_uses_incremental_window_when_db_has_data() -> None:
    last = datetime.now(UTC) - timedelta(minutes=10)
    db = FakeDb(latest_open=last)
    service = CandleSyncService()
    spy = _SpyClient()
    service.client = spy  # type: ignore[assignment]

    await service.sync_market(db, _market(), "5m", lookback=300)

    interval_ms = TIMEFRAME_MS["5m"]
    requested_window_ms = spy.captured["end_ms"] - spy.captured["start_ms"]
    # Incremental: window is from last - one_bar to now → roughly 10min + one bar
    # Way smaller than 300 bars (300*5min = 25h).
    assert requested_window_ms < interval_ms * 50
    assert spy.captured["start_ms"] == int(last.timestamp() * 1000) - interval_ms


@pytest.mark.asyncio
async def test_prefetch_skips_spot_markets_and_uses_configured_attempts() -> None:
    db = FakeDb(latest_open=None)
    service = CandleSyncService()
    spy = _SpyClient()
    service.client = spy  # type: ignore[assignment]
    service._latest_open_map = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]

    result = await service.prefetch(
        db,
        [
            (_market(market_id=1, market_type="perp", symbol="BTC"), "5m"),
            (_market(market_id=2, market_type="spot", symbol="PURR/USDC"), "5m"),
        ],
        attempts=2,
    )

    assert set(result) == {(1, "5m")}
    assert len(spy.calls) == 1
    assert spy.calls[0]["coin"] == "BTC"
    assert spy.calls[0]["attempts"] == 2
