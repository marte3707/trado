from enum import StrEnum


class OperationMode(StrEnum):
    SIGNAL_ONLY = "SIGNAL_ONLY"
    PAPER_TRADING = "PAPER_TRADING"
    AUTO_TRADE = "AUTO_TRADE"


class MarketType(StrEnum):
    SPOT = "spot"
    PERP = "perp"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    STRONG_LONG = "STRONG_LONG"
    STRONG_SHORT = "STRONG_SHORT"


class SignalStatus(StrEnum):
    NEW = "new"
    ACTIVE = "active"
    IGNORED = "ignored"
    EXECUTED = "executed"
    CLOSED = "closed"
    INVALIDATED = "invalidated"


class OrderMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"

