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
    api_key: str | None = None
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    enabled_timeframes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["5m", "15m", "1h", "4h"]
    )
    min_signal_score: int = 60
    min_risk_reward: float = 2.0
    min_average_quote_volume_usd: float = 50_000.0

    hyperliquid_network: Literal["mainnet", "testnet"] = "testnet"
    hyperliquid_account_address: str | None = None
    hyperliquid_private_key: str | None = None
    # Master kill-switch on live trading. Must be `true` AND operation_mode must
    # be AUTO_TRADE for live orders to leave the engine.
    enable_live_trading: bool = False
    # Separate explicit opt-in required to run live trades on mainnet. Testnet
    # is allowed when only `enable_live_trading` is true.
    enable_mainnet_live_trading: bool = False
    # Hyperliquid account equity must be at least this many USD before
    # OrderValidator will authorize any live order. Acts as a basic floor so a
    # near-empty account cannot trigger fills.
    live_min_account_value_usd: float = 100.0
    # Maximum notional (price × size, USD) per single live order. Hard cap that
    # overrides any sizing computed from risk_pct/leverage.
    live_order_notional_cap_usd: float = 250.0
    # Hyperliquid rejects any order whose notional is below this floor (the
    # exchange-side minimum is $10). Used by validator + sizing logic.
    live_min_order_notional_usd: float = 10.0
    # When the risk-based size produces a notional below the min floor, either
    # reject the order (false, default) or bump the size up to the floor
    # — knowingly violating risk_pct to satisfy the exchange minimum.
    live_bump_to_min_notional: bool = False
    # Max slippage tolerated on market_open / market_close, expressed in
    # percent (0.5 means 0.5%). Forwarded to the Hyperliquid SDK.
    live_max_slippage_pct: float = 0.5
    # TTL of the signed order request submitted to Hyperliquid. If the order
    # is not picked up within this window the exchange rejects it.
    live_order_expires_after_seconds: int = 30

    coinmarketcap_api_key: str | None = None
    coinmarketcap_enabled: bool = True
    coinmarketcap_base_url: str = "https://pro-api.coinmarketcap.com"
    coinmarketcap_quota_cooldown_seconds: int = 86_400
    coinmarketcap_usage_cache_ttl_seconds: int = 300
    coinmarketcap_min_credits_left: int = 1
    external_quote_enrichment_enabled: bool = False
    external_market_data_cache_ttl_seconds: int = 21_600
    coingecko_enabled: bool = True
    coingecko_api_key: str | None = None
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
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

    funding_rate_weight: int = 5
    max_long_funding_rate_pct: float = 0.05  # 0.05%/8h → blocca long oltre questa soglia

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

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
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
