from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32), default="hyperliquid", index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    base: Mapped[str] = mapped_column(String(32))
    quote: Mapped[str] = mapped_column(String(32))
    market_type: Mapped[str] = mapped_column(String(16), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    candles: Mapped[list["Candle"]] = relationship(back_populates="market")

    __table_args__ = (UniqueConstraint("exchange", "symbol", "market_type", name="uq_market"),)


class Candle(Base):
    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    open_interest: Mapped[float | None] = mapped_column(Float(), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="hyperliquid")

    market: Mapped[Market] = relationship(back_populates="candles")

    __table_args__ = (
        UniqueConstraint("market_id", "timeframe", "open_time", name="uq_candle_market_tf_time"),
        # ix_candles_market_tf_time rimosso: identico alla unique constraint sopra
    )


class Indicator(Base):
    __tablename__ = "indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # values = { indicator_name: { key: val, "_direction": ..., ... }, ... }
    values: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("market_id", "timeframe", "timestamp", name="uq_indicators_market_tf_time"),
        Index("ix_indicators_market_tf_time", "market_id", "timeframe", "timestamp"),
    )


class VolumeProfileCache(Base):
    __tablename__ = "volume_profile_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(32), default="ohlcv", index=True)
    levels: Mapped[list] = mapped_column(JSONB, default=list)
    poc_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    vah_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    val_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    total_volume: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped[Market] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "market_id",
            "timeframe",
            "window_start",
            "window_end",
            "source",
            name="uq_volume_profile_cache_window",
        ),
        Index(
            "ix_volume_profile_cache_market_tf_source_end",
            "market_id",
            "timeframe",
            "source",
            "window_end",
        ),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    direction: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[int] = mapped_column(Integer, index=True)
    confidence_label: Mapped[str] = mapped_column(String(32))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    take_profit_1: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    take_profit_2: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    take_profit_3: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    risk_reward: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    strategy_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    source_candle_open_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    market: Mapped[Market] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "market_id", "timeframe", "source_candle_open_time", name="uq_signal_market_tf_candle"
        ),
        Index("ix_signals_market_tf_created", "market_id", "timeframe", "created_at"),
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    hyperliquid_position_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    size: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    margin_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    pnl_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cum_funding_since_open: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    market: Mapped[Market] = relationship()

    __table_args__ = (
        # Copre il count posizioni live aperte nell'OrderValidator (status+mode)
        # e i filtri per stato/modalità della dashboard.
        Index("ix_positions_status_mode", "status", "mode"),
    )


class PositionStopUpdate(Base):
    __tablename__ = "position_stop_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="CASCADE"), index=True
    )
    old_stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    new_stop_loss: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    current_price: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), index=True)
    indicators: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    position: Mapped[Position] = relationship()

    __table_args__ = (
        Index("ix_position_stop_updates_position_created", "position_id", "created_at"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    hyperliquid_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cloid: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    order_type: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    size: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    status: Mapped[str] = mapped_column(String(32), index=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    raw_response: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Join orders→position filtrati per stato (reconciliation, dashboard).
        Index("ix_orders_position_status", "position_id", "status"),
    )


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    weight: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("strategy_name", "timeframe", name="uq_strategy_tf"),)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    effective_account_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    free_spot_usdc: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    available_balance: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    margin_used: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    raw_data: Mapped[dict] = mapped_column(JSONB, default=dict)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    source: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trade_capital: Mapped[str | None] = mapped_column(String(16), nullable=True)
    onboarding_stage: Mapped[str] = mapped_column(String(32), default="started", index=True)
    risk_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invite_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    joined_channel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DecisionLog(Base):
    __tablename__ = "decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
