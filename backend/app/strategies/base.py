from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from app.core.enums import Direction


@dataclass(frozen=True)
class CandleInput:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float | None = None


@dataclass(frozen=True)
class StrategyContext:
    market: str
    symbol: str
    timeframe: str
    market_type: str
    candles: list[CandleInput]


@dataclass
class StrategyResult:
    strategy_name: str
    market: str
    symbol: str
    timeframe: str
    direction: Direction
    confidence: int
    reason: str
    indicator_values: dict
    suggested_entry: float | None = None
    suggested_stop_loss: float | None = None
    suggested_take_profit: float | None = None
    invalidation_level: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Strategy(Protocol):
    name: str

    def evaluate(self, context: StrategyContext, params: dict) -> StrategyResult:
        raise NotImplementedError

