from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.runtime_settings import DEFAULT_POSITION_MANAGEMENT_SETTINGS
from app.db.models import AppSetting, Base, StrategyConfig
from app.db.session import engine
from app.services.unified_account_ratio import DEFAULT_UAR_RISK_SETTINGS


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
    "oi_trend": {"min_candles": 5, "oi_change_threshold": 0.02, "price_change_threshold": 0.001},
    "cvd_divergence": {"lookback": 30, "min_divergence_pct": 0.005},
    "liquidation_cascade": {
        "lookback": 50,
        "volume_factor": 2.5,
        "range_factor": 2.0,
        "wick_min_ratio": 0.55,
        "close_position_threshold": 0.35,
    },
    "volume_profile": {
        "lookback": 100,
        "bins": 30,
        "proximity_pct": 0.004,
        "hvn_percentile": 0.80,
    },
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
    # wyckoff: WR ~54% nei dati storici — peso aumentato da 12
    "wyckoff_spring_upthrust": 15,
    # oi_trend: WR 25%, PF 0.21 — peso ridotto da 10
    "oi_trend": 4,
    # cvd_divergence: WR 50% — peso aumentato da 8
    "cvd_divergence": 10,
    # liquidation_cascade: segnale raro, pochi dati — peso ridotto da 10
    "liquidation_cascade": 5,
    "volume_profile": 9,
}

LEGACY_DEFAULT_STRATEGY_WEIGHTS = {
    "ema_crossover": 30,
    "rsi": 20,
    "macd": 25,
    "volume_spike": 25,
}

DEFAULT_EDGE_RISK_SETTINGS = {
    "directional_score_adjustments": {},
    "score_bucket_adjustments": {},
    "market_score_adjustments": {},
    "timeframe_score_adjustments": {},
    "allow_ob_imbalance_only_signals": False,
    "entry_quality_filter_enabled": True,
    "entry_quality_ema_period": 21,
    "entry_quality_long_ob_imbalance_min": 0.05,
    "entry_quality_short_ob_imbalance_max": 0.0,
    "max_combined_score_adjustment": 15,
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
                    "close_companion_paper_on_live_reconcile": False,
                },
            )
        )
    else:
        live_execution = db.get(AppSetting, "live_execution")
        if live_execution is not None and isinstance(live_execution.value, dict):
            value = dict(live_execution.value)
            if "close_companion_paper_on_live_reconcile" not in value:
                value["close_companion_paper_on_live_reconcile"] = False
                live_execution.value = value
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
                    **DEFAULT_EDGE_RISK_SETTINGS,
                    **DEFAULT_UAR_RISK_SETTINGS,
                },
            )
        )
    else:
        risk_setting = db.get(AppSetting, "risk")
        if risk_setting is not None and isinstance(risk_setting.value, dict):
            value = dict(risk_setting.value)
            mutated = False
            for key, default in DEFAULT_UAR_RISK_SETTINGS.items():
                if key not in value:
                    value[key] = default
                    mutated = True
            for key, default in DEFAULT_EDGE_RISK_SETTINGS.items():
                if key not in value:
                    value[key] = default
                    mutated = True
            if mutated:
                risk_setting.value = value
    if db.get(AppSetting, "position_management") is None:
        db.add(
            AppSetting(
                key="position_management",
                value=DEFAULT_POSITION_MANAGEMENT_SETTINGS,
            )
        )
    else:
        existing_pm = db.get(AppSetting, "position_management")
        if existing_pm is not None and isinstance(existing_pm.value, dict):
            value = dict(existing_pm.value)
            if "timeStop" not in value:
                value["timeStop"] = DEFAULT_POSITION_MANAGEMENT_SETTINGS["timeStop"]
                existing_pm.value = value
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
                    "min_strategy_trades": 20,
                    "min_weight": 1,
                    "max_weight": 30,
                    "min_signal_score_floor": 55,
                    "min_signal_score_ceiling": 80,
                    "min_risk_reward_floor": 2.0,
                    "min_risk_reward_ceiling": 3.0,
                    "min_risk_per_trade_pct": 0.25,
                    "deep_defensive_slope_threshold": -0.1,
                    "auto_disable_retest_after_trades": 200,
                },
            )
        )
    else:
        # Idempotent upgrade: il default della slope threshold è cambiato da -2.0
        # (USD/trade-index, dipendente dalla scala) a -0.1 (unit-less, normalizzato
        # per |PnL| mediano). Aggiorna SOLO se il valore in DB è il vecchio default.
        existing = db.get(AppSetting, "auto_calibration")
        if existing is not None and isinstance(existing.value, dict):
            updated = dict(existing.value)
            mutated = False
            if updated.get("deep_defensive_slope_threshold") == -2.0:
                updated["deep_defensive_slope_threshold"] = -0.1
                mutated = True
            if updated.get("min_strategy_trades") == 8:
                updated["min_strategy_trades"] = 20
                mutated = True
            if "auto_disable_retest_after_trades" not in updated:
                updated["auto_disable_retest_after_trades"] = 200
                mutated = True
            if "deep_defensive_cooldown_runs" not in updated:
                updated["deep_defensive_cooldown_runs"] = 6
                mutated = True
            if "deep_defensive_cooldown_min_new_trades" not in updated:
                updated["deep_defensive_cooldown_min_new_trades"] = 3
                mutated = True
            if mutated:
                existing.value = updated
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
                    "reverse_mode_enabled": False,
                    "reverse_cooldown_minutes": 240,
                    "reverse_max_per_day": 3,
                    "reverse_signal_flip_max_age_minutes": 60,
                    "reverse_signal_flip_min_score": 50,
                    "_reverse_history": {},
                },
            )
        )
    else:
        existing = db.get(AppSetting, "btc_macro_protection")
        if existing is not None and isinstance(existing.value, dict):
            updated = dict(existing.value)
            mutated = False
            reverse_defaults = {
                "reverse_mode_enabled": False,
                "reverse_cooldown_minutes": 240,
                "reverse_max_per_day": 3,
                "reverse_signal_flip_max_age_minutes": 60,
                "reverse_signal_flip_min_score": 50,
                "_reverse_history": {},
            }
            for key, default in reverse_defaults.items():
                if key not in updated:
                    updated[key] = default
                    mutated = True
            if mutated:
                existing.value = updated
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
