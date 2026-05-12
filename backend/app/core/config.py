from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.enums import OperationMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    operation_mode: OperationMode = OperationMode.SIGNAL_ONLY
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/trado"
    enabled_timeframes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["5m", "15m", "1h", "4h"]
    )
    min_signal_score: int = 60
    min_risk_reward: float = 2.0
    min_average_quote_volume_usd: float = 50_000.0

    hyperliquid_network: Literal["mainnet", "testnet"] = "testnet"
    hyperliquid_account_address: str | None = None
    hyperliquid_private_key: str | None = None
    enable_live_trading: bool = False

    coinmarketcap_api_key: str | None = None
    coinmarketcap_base_url: str = "https://pro-api.coinmarketcap.com"
    external_market_data_cache_ttl_seconds: int = 300
    fear_greed_weight: int = 4
    altcoin_season_weight: int = 4

    whale_alert_api_key: str | None = None
    whale_alert_base_url: str = "https://api.whale-alert.io/v1"
    whale_alert_public_url: str = "https://whale-alert.io/data.json"
    whale_alert_public_alerts: int = 10
    whale_alert_public_cache_ttl_seconds: int = 600
    whale_alert_min_value_usd: int = 500_000
    whale_alert_lookback_minutes: int = 60
    whale_flow_weight: int = 6
    whale_contra_block_confidence: int = 70

    kill_switch_enabled: bool = False
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_open_positions: int = 5
    max_total_exposure_pct: float = 25.0
    max_leverage: float = 3.0
    paper_equity: float = 10_000.0

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    vapid_private_key: str | None = None
    vapid_public_key: str | None = None
    vapid_subject: str = "mailto:admin@trado.app"

    @field_validator("enabled_timeframes", mode="before")
    @classmethod
    def parse_timeframes(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def info_url(self) -> str:
        if self.hyperliquid_network == "mainnet":
            return "https://api.hyperliquid.xyz/info"
        return "https://api.hyperliquid-testnet.xyz/info"

    @property
    def exchange_url(self) -> str:
        if self.hyperliquid_network == "mainnet":
            return "https://api.hyperliquid.xyz/exchange"
        return "https://api.hyperliquid-testnet.xyz/exchange"

    @property
    def ws_url(self) -> str:
        if self.hyperliquid_network == "mainnet":
            return "wss://api.hyperliquid.xyz/ws"
        return "wss://api.hyperliquid-testnet.xyz/ws"

    @property
    def live_trading_allowed(self) -> bool:
        return self.operation_mode == OperationMode.AUTO_TRADE and self.enable_live_trading


@lru_cache
def get_settings() -> Settings:
    return Settings()
