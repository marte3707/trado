from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exchange: str
    symbol: str
    base: str
    quote: str
    market_type: str
    active: bool
    metadata_json: dict[str, Any]


class CandleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class IndicatorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    indicator_name: str
    timeframe: str
    timestamp: datetime
    values: dict[str, Any]


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    market_id: int
    market: MarketOut
    timeframe: str
    direction: str
    score: int
    confidence_label: str
    entry_price: Decimal
    stop_loss: Decimal | None
    take_profit_1: Decimal | None
    take_profit_2: Decimal | None
    take_profit_3: Decimal | None
    risk_reward: Decimal | None
    reason: str
    strategy_breakdown: dict[str, Any]
    status: str
    source_candle_open_time: datetime | None = None
    created_at: datetime
    updated_at: datetime
    calibrated_expected_r: float | None = None
    calibrated_win_rate: float | None = None
    calibration_n_samples: int | None = None
    calibration_confidence: str | None = None


class DecisionLogOut(BaseModel):
    id: int
    timestamp: datetime
    event_type: str
    market_id: int | None = None
    signal_id: int | None = None
    position_id: int | None = None
    message: str
    data: dict[str, Any]


class SignalSnapshotOut(BaseModel):
    signal: SignalOut
    source_candle_open_time: datetime | None
    candles: list[CandleOut]
    indicators: list[IndicatorOut]
    positions: list[dict[str, Any]]
    decision_logs: list[DecisionLogOut]


class SettingsPatch(BaseModel):
    value: dict[str, Any]


class ModeLimitSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    paper: float | None = Field(default=None, ge=0)
    live: float | None = Field(default=None, ge=0)


class LowTimeframeDirectionalSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    timeframes: list[str] | None = None
    apply_timeframes: list[str] | None = None
    paper: float | None = Field(default=None, ge=0)
    live: float | None = Field(default=None, ge=0)


class PortfolioGuardSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    max_new_positions_per_cycle_by_side: ModeLimitSettings | None = None
    max_directional_open_positions: ModeLimitSettings | None = None
    max_directional_markets: ModeLimitSettings | None = None
    max_directional_risk_r: ModeLimitSettings | None = None
    max_low_tf_directional_positions: LowTimeframeDirectionalSettings | None = None
    max_low_tf_directional_open_positions: LowTimeframeDirectionalSettings | None = None


class CircuitBreakerSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    window_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_loss_r: float | None = Field(default=None, ge=0)
    max_stop_losses: int | None = Field(default=None, ge=0)
    pause_minutes: int | None = Field(default=None, ge=1, le=1440)


class DirectionalReversalBreakerSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    modes: dict[str, bool] | None = None
    apply_timeframes: list[str] | None = None
    window_minutes: int | None = Field(default=None, ge=1, le=1440)
    stop_loss_count: int | None = Field(default=None, ge=1, le=100)
    cooldown_minutes: int | None = Field(default=None, ge=1, le=1440)


class RiskControlsSettings(BaseModel):
    """Validazione non distruttiva dei campi di sicurezza di AppSetting "risk".

    `extra="allow"` preserva i campi non modellati (overrides, flag, liste): qui
    vincoliamo solo i parametri numerici critici per evitare configurazioni
    pericolose (percentuali negative, score fuori scala, leva assurda)."""

    model_config = ConfigDict(extra="allow")

    min_signal_score: int | None = Field(default=None, ge=0, le=100)
    min_risk_reward: float | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, ge=0)
    max_daily_loss_pct: float | None = Field(default=None, ge=0, le=100)
    max_total_exposure_pct: float | None = Field(default=None, ge=0, le=100)
    max_leverage: float | None = Field(default=None, gt=0, le=100)
    portfolio_guard: PortfolioGuardSettings | None = None
    circuit_breaker: CircuitBreakerSettings | None = None
    directional_reversal_breaker: DirectionalReversalBreakerSettings | None = None


class LiveExecutionSettings(BaseModel):
    """Validazione non distruttiva dei parametri di esecuzione live (AppSetting
    "live_execution"). Vedi `RiskControlsSettings` per il razionale di `extra`."""

    model_config = ConfigDict(extra="allow")

    new_live_entries_enabled: bool | None = None
    readiness_gate_enabled: bool | None = None
    readiness_lookback_trades: int | None = Field(default=None, ge=20, le=5000)
    readiness_recent_trades: int | None = Field(default=None, ge=10, le=1000)
    readiness_min_closed_trades: int | None = Field(default=None, ge=10, le=5000)
    readiness_min_recent_trades: int | None = Field(default=None, ge=5, le=1000)
    readiness_min_profit_factor: float | None = Field(default=None, ge=0, le=10)
    readiness_min_recent_profit_factor: float | None = Field(default=None, ge=0, le=10)
    readiness_min_avg_net_r: float | None = Field(default=None, ge=-5, le=5)
    readiness_min_recent_avg_net_r: float | None = Field(default=None, ge=-5, le=5)
    readiness_max_drawdown_r: float | None = Field(default=None, ge=0, le=1000)
    min_account_value_usd: float | None = Field(default=None, ge=0)
    order_notional_cap_usd: float | None = Field(default=None, ge=0)
    min_order_notional_usd: float | None = Field(default=None, ge=0)
    min_reduce_only_notional_usd: float | None = Field(default=None, ge=0)
    max_slippage_pct: float | None = Field(default=None, ge=0, le=100)
    order_expires_after_seconds: int | None = Field(default=None, ge=0)


class TheBullSettings(BaseModel):
    """Fail-closed runtime controls for the external Telegram signal source."""

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    channel_username: str | None = Field(
        default=None,
        min_length=5,
        max_length=64,
        pattern=r"^@?[A-Za-z0-9_]+$",
    )
    execution_mode: Literal["paper", "live"] | None = None
    timeframe: Literal["1h", "4h", "1d"] | None = None
    feed_history_limit: int | None = Field(default=None, ge=10, le=100)
    max_signal_age_minutes: int | None = Field(default=None, ge=1, le=120)
    signal_score: int | None = Field(default=None, ge=1, le=100)
    risk_multiplier: float | None = Field(default=None, ge=0.05, le=1.0)
    max_open_positions: int | None = Field(default=None, ge=1, le=20)
    max_additions_per_ticker: int | None = Field(default=None, ge=0, le=2)
    max_addition_fraction: float | None = Field(default=None, gt=0, le=0.30)
    min_stop_distance_pct: float | None = Field(default=None, ge=0.005, le=0.20)
    close_on_profit_update: bool | None = None
    close_companion_paper_on_source_exit: bool | None = None
    semantic_audit_enabled: bool | None = None
    expanded_entry_execution: bool | None = None
    chart_analysis_enabled: bool | None = None
    chart_zone_execution: bool | None = None
    chart_ocr_fallback_execution: bool | None = None
    chart_analysis_max_per_cycle: int | None = Field(default=None, ge=1, le=5)
    spot_fallback_enabled: bool | None = None


class StrategyPatch(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] | None = None
    weight: int | None = None


class PushSubscribePayload(BaseModel):
    endpoint: str
    keys: dict[str, str]
    expirationTime: float | None = None


class PushUnsubscribePayload(BaseModel):
    endpoint: str
