from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.runtime_settings import DEFAULT_POSITION_MANAGEMENT_SETTINGS
from app.db.models import AppSetting, Base, StrategyConfig
from app.db.session import engine


DEFAULT_STRATEGIES = {
    "ema_crossover": {"fast": 9, "slow": 21, "trend": 200},
    "rsi": {"period": 14, "oversold": 30, "overbought": 70},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "volume_spike": {"period": 20, "multiplier": 1.5},
    "fibonacci": {"lookback": 120, "tolerance_pct": 0.004},
    "support_resistance": {"lookback": 120, "pivot_window": 3, "tolerance_pct": 0.0035},
    "supertrend": {"period": 10, "multiplier": 3.0},
    "bollinger_bands": {"period": 20, "stddev": 2.0},
    "vwap": {"period": 48, "tolerance_pct": 0.002},
    "candlestick_patterns": {},
    "wyckoff_spring_upthrust": {"lookback": 50, "pivot_window": 5, "tolerance_pct": 0.002, "volume_factor": 1.3},
}

DEFAULT_STRATEGY_WEIGHTS = {
    "ema_crossover": 14,
    "rsi": 9,
    "macd": 12,
    "volume_spike": 7,
    "fibonacci": 9,
    "support_resistance": 13,
    "supertrend": 15,
    "bollinger_bands": 8,
    "vwap": 8,
    "candlestick_patterns": 5,
    "wyckoff_spring_upthrust": 12,
}

LEGACY_DEFAULT_STRATEGY_WEIGHTS = {
    "ema_crossover": 30,
    "rsi": 20,
    "macd": 25,
    "volume_spike": 25,
}


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def seed_defaults(db: Session) -> None:
    settings = get_settings()
    if db.get(AppSetting, "operation_mode") is None:
        db.add(
            AppSetting(
                key="operation_mode",
                value={"mode": settings.operation_mode.value, "enable_live_trading": settings.enable_live_trading},
            )
        )
    else:
        operation_mode = db.get(AppSetting, "operation_mode")
        if operation_mode is not None and "enable_live_trading" not in operation_mode.value:
            operation_mode.value = {**operation_mode.value, "enable_live_trading": settings.enable_live_trading}
    if db.get(AppSetting, "kill_switch") is None:
        db.add(AppSetting(key="kill_switch", value={"enabled": settings.kill_switch_enabled}))
    if db.get(AppSetting, "paper") is None:
        db.add(AppSetting(key="paper", value={"equity": settings.paper_equity}))
    if db.get(AppSetting, "live_execution") is None:
        db.add(
            AppSetting(
                key="live_execution",
                value={
                    "min_account_value_usd": settings.live_min_account_value_usd,
                    "order_notional_cap_usd": settings.live_order_notional_cap_usd,
                    "max_slippage_pct": settings.live_max_slippage_pct,
                    "order_expires_after_seconds": settings.live_order_expires_after_seconds,
                    "enable_mainnet_live_trading": settings.enable_mainnet_live_trading,
                },
            )
        )
    if db.get(AppSetting, "risk") is None:
        db.add(
            AppSetting(
                key="risk",
                value={
                    "min_signal_score": settings.min_signal_score,
                    "min_risk_reward": max(settings.min_risk_reward, 2.0),
                    "min_average_quote_volume_usd": settings.min_average_quote_volume_usd,
                    "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
                    "max_daily_loss_pct": settings.max_daily_loss_pct,
                    "max_open_positions": settings.max_open_positions,
                    "max_total_exposure_pct": settings.max_total_exposure_pct,
                    "max_leverage": settings.max_leverage,
                },
            )
        )
    if db.get(AppSetting, "position_management") is None:
        db.add(
            AppSetting(
                key="position_management",
                value=DEFAULT_POSITION_MANAGEMENT_SETTINGS,
            )
        )
    if db.get(AppSetting, "timeframes") is None:
        db.add(AppSetting(key="timeframes", value={"enabled": settings.enabled_timeframes}))
    if db.get(AppSetting, "alerts") is None:
        db.add(
            AppSetting(
                key="alerts",
                value={
                    "dashboard_enabled": True,
                    "telegram_enabled": bool(settings.telegram_bot_token and settings.telegram_chat_id),
                    "browser_push_enabled": False,
                    "min_score": settings.min_signal_score,
                    "only_strong": True,
                    "position_state_alerts": False,
                    "risk_warnings": True,
                    "funding_threshold_pct": None,
                    "mute_during_auto_trade": True,
                    "quiet_from": None,
                    "quiet_to": None,
                },
            )
        )
    if db.get(AppSetting, "auto_calibration") is None:
        db.add(
            AppSetting(
                key="auto_calibration",
                value={
                    "enabled": True,
                    "lookback_trades": 500,
                    "min_closed_trades": 20,
                    "min_strategy_trades": 8,
                    "min_weight": 1,
                    "max_weight": 30,
                    "min_signal_score_floor": 55,
                    "min_signal_score_ceiling": 80,
                    "min_risk_reward_floor": 2.0,
                    "min_risk_reward_ceiling": 3.0,
                    "min_risk_per_trade_pct": 0.25,
                },
            )
        )
    if db.get(AppSetting, "btc_macro_protection") is None:
        db.add(
            AppSetting(
                key="btc_macro_protection",
                value={
                    "enabled": False,
                    "timeframe": "5m",
                    "lookback_candles": 6,
                    "reversal_pct_threshold": 2.0,
                    "volume_multiplier": 1.2,
                    "breakeven_buffer_pct": 0.3,
                    "loss_exit_enabled": True,
                    "require_ema_confirmation": True,
                    "confirmation_timeframe": "1h",
                    "confirmation_ema_period": 200,
                    "_last_btc_trend": "NEUTRAL",
                    "_last_directional_trend": "NEUTRAL",
                    "_last_reversal_at": None,
                },
            )
        )
    for timeframe in settings.enabled_timeframes:
        seed_timeframe(db, timeframe)
    db.commit()


def seed_timeframe(db: Session, timeframe: str) -> None:
    for strategy_name, params in DEFAULT_STRATEGIES.items():
        exists = (
            db.query(StrategyConfig)
            .filter(
                StrategyConfig.strategy_name == strategy_name,
                StrategyConfig.timeframe == timeframe,
            )
            .first()
        )
        if exists is None:
            db.add(
                StrategyConfig(
                    strategy_name=strategy_name,
                    timeframe=timeframe,
                    enabled=True,
                    params=params,
                    weight=DEFAULT_STRATEGY_WEIGHTS[strategy_name],
                )
            )
        elif (
            strategy_name in LEGACY_DEFAULT_STRATEGY_WEIGHTS
            and exists.weight == LEGACY_DEFAULT_STRATEGY_WEIGHTS[strategy_name]
        ):
            exists.weight = DEFAULT_STRATEGY_WEIGHTS[strategy_name]
