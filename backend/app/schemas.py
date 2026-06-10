from datetime import datetime
from decimal import Decimal
from typing import Any

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


class LiveExecutionSettings(BaseModel):
    """Validazione non distruttiva dei parametri di esecuzione live (AppSetting
    "live_execution"). Vedi `RiskControlsSettings` per il razionale di `extra`."""

    model_config = ConfigDict(extra="allow")

    min_account_value_usd: float | None = Field(default=None, ge=0)
    order_notional_cap_usd: float | None = Field(default=None, ge=0)
    min_order_notional_usd: float | None = Field(default=None, ge=0)
    max_slippage_pct: float | None = Field(default=None, ge=0, le=100)
    order_expires_after_seconds: int | None = Field(default=None, ge=0)


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
