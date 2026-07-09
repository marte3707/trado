from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.runtime_settings import (
    DEFAULT_DIRECTIONAL_REVERSAL_BREAKER_SETTINGS,
    DEFAULT_MACRO_RISK_MODE_SETTINGS,
    DEFAULT_MACRO_RISK_MONITOR_SETTINGS,
    DEFAULT_MACRO_RISK_STATE,
    DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_SETTINGS,
    DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE,
    DEFAULT_PORTFOLIO_GUARD_SETTINGS,
    DEFAULT_POSITION_MANAGEMENT_SETTINGS,
)
from app.db.models import AppSetting, Base, Market, StrategyConfig
from app.db.session import engine
from app.engine.pre_tp1_validation import DEFAULT_PRE_TP1_SETTINGS
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
        "lvn_percentile": 0.20,
        "value_area_pct": 0.70,
    },
    "trend_following_adaptive": {
        "fast_ema": 21,
        "slow_ema": 55,
        "trend_ema": 200,
        "donchian_lookback": 55,
        "adx_period": 14,
        "min_adx": 18.0,
        "atr_period": 14,
        "atr_stop_multiplier": 2.5,
        "reward_r_multiple": 2.0,
        "volume_lookback": 20,
        "min_volume_ratio": 0.8,
        "breakout_atr_buffer": 0.05,
    },
}

DEFAULT_DISABLED_STRATEGIES = {
    # New strategy candidates must be explicitly enabled or tested before they
    # influence scoring/live paths.
    "trend_following_adaptive",
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
    "trend_following_adaptive": 10,
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
    # Soglie min score per-timeframe (solo al rialzo rispetto a min_signal_score):
    # stringe i timeframe con edge negativo documentato senza toccare gli altri.
    "timeframe_min_score_overrides": {},
    # Probation per mercati senza storico: sotto min_closed_trades posizioni
    # chiuse il rischio per trade è scalato di size_multiplier. Taglia la coda
    # dei mercati one-shot che bruciano stop pieni al primo segnale.
    "new_market_probation": {
        "enabled": True,
        "min_closed_trades": 3,
        "size_multiplier": 0.5,
    },
    # Sospende (non cancella) le penalità direzionali apprese in regime macro
    # opposto: con macro BTC bullish, blocklist/penalità/override LONG non si
    # applicano finché il regime resta favorevole. Vedi engine/macro_direction.py.
    "regime_aware_directional_penalties": True,
    "allow_ob_imbalance_only_signals": False,
    "entry_quality_filter_enabled": True,
    "entry_quality_ema_period": 21,
    "entry_quality_long_ob_imbalance_min": 0.05,
    "entry_quality_short_ob_imbalance_max": 0.0,
    **DEFAULT_PRE_TP1_SETTINGS,
    "max_combined_score_adjustment": 15,
    "btc_volume_profile_gate_enabled": True,
    "btc_volume_profile_exchange": "HYPERLIQUID_FUTURES",
    "btc_volume_profile_raw_symbol": "BTC",
    "btc_volume_profile_interval": "HOUR",
    "btc_volume_profile_lookback_hours": 48,
    "btc_volume_profile_price_timeframe": "1h",
    "btc_volume_profile_proximity_pct": 0.004,
    "btc_volume_profile_risk_off_long_penalty": 6,
    "btc_volume_profile_risk_on_short_penalty": 6,
    "btc_volume_profile_rejection_penalty": 4,
    "btc_volume_profile_poc_chop_penalty": 0,
}


def _normalize_macro_risk_monitor_policies(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, dict):
        return value, False
    raw_policies = value.get("policies")
    if not isinstance(raw_policies, dict):
        return value, False
    policies = dict(raw_policies)
    mutated = False
    for level in ("watch", "high"):
        raw_policy = raw_policies.get(level)
        if not isinstance(raw_policy, dict):
            continue
        if raw_policy.get("require_mtf_confluence") is True:
            policies[level] = {
                **raw_policy,
                "require_mtf_confluence": False,
            }
            mutated = True
    if not mutated:
        return value, False
    return {**value, "policies": policies}, True


# Market osservati solo a scopo macro: sincronizzano candele ma NON generano
# segnali né posizioni (vedi guard `monitoring_only` nel worker). XYZ100 è il
# perp HIP-3 di trade[XYZ] su Hyperliquid che traccia il Nasdaq-100 (coin API
# `xyz:XYZ100`); non compare nella meta() standard, quindi va seedato a parte.
MONITORING_MARKETS = [
    {
        "symbol": "XYZ100",
        "base": "XYZ100",
        "quote": "USDC",
        "market_type": "perp",
        "metadata_json": {"hl_coin": "xyz:XYZ100", "monitoring_only": True, "dex": "xyz"},
    },
]


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
                    "min_order_notional_usd": settings.live_min_order_notional_usd,
                    "min_reduce_only_notional_usd": 10.0,
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
            if "min_reduce_only_notional_usd" not in value:
                value["min_reduce_only_notional_usd"] = 10.0
            if "min_order_notional_usd" not in value:
                value["min_order_notional_usd"] = settings.live_min_order_notional_usd
            if value != live_execution.value:
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
                    "portfolio_guard": DEFAULT_PORTFOLIO_GUARD_SETTINGS,
                    "circuit_breaker": DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_SETTINGS,
                    "directional_reversal_breaker": DEFAULT_DIRECTIONAL_REVERSAL_BREAKER_SETTINGS,
                    "macro_risk_mode": DEFAULT_MACRO_RISK_MODE_SETTINGS,
                    "macro_risk_monitor": DEFAULT_MACRO_RISK_MONITOR_SETTINGS,
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
            portfolio_defaults = {
                "portfolio_guard": DEFAULT_PORTFOLIO_GUARD_SETTINGS,
                "circuit_breaker": DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_SETTINGS,
                "directional_reversal_breaker": DEFAULT_DIRECTIONAL_REVERSAL_BREAKER_SETTINGS,
                "macro_risk_mode": DEFAULT_MACRO_RISK_MODE_SETTINGS,
                "macro_risk_monitor": DEFAULT_MACRO_RISK_MONITOR_SETTINGS,
            }
            for key, default in portfolio_defaults.items():
                if key not in value:
                    value[key] = default
                    mutated = True
            normalized_monitor, normalized = _normalize_macro_risk_monitor_policies(
                value.get("macro_risk_monitor")
            )
            if normalized:
                value["macro_risk_monitor"] = normalized_monitor
                mutated = True
            if mutated:
                risk_setting.value = value
    if db.get(AppSetting, "portfolio_circuit_breaker_state") is None:
        db.add(
            AppSetting(
                key="portfolio_circuit_breaker_state",
                value=DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE,
            )
        )
    if db.get(AppSetting, "macro_risk_state") is None:
        db.add(
            AppSetting(
                key="macro_risk_state",
                value=DEFAULT_MACRO_RISK_STATE,
            )
        )
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
        db.add(
            AppSetting(
                key="timeframes",
                value={"enabled": settings.enabled_timeframes, "monitoring_only": []},
            )
        )
    else:
        existing_timeframes = db.get(AppSetting, "timeframes")
        if existing_timeframes is not None and isinstance(existing_timeframes.value, dict):
            value = dict(existing_timeframes.value)
            if "monitoring_only" not in value:
                value["monitoring_only"] = []
                existing_timeframes.value = value
    if db.get(AppSetting, "market_data_ws") is None:
        db.add(
            AppSetting(
                key="market_data_ws",
                value={
                    "enabled": True,
                    "max_subscriptions": 950,
                    "subscription_refresh_seconds": 60,
                    "heartbeat_seconds": 20,
                    "reconnect_max_seconds": 60,
                    "writer_flush_seconds": 1,
                    "writer_batch_size": 500,
                },
            )
        )
    if db.get(AppSetting, "market_data_ws_state") is None:
        db.add(
            AppSetting(
                key="market_data_ws_state",
                value={
                    "connected": False,
                    "ready": False,
                    "active_subscriptions": 0,
                    "expected_subscriptions": 0,
                    "pending_events": 0,
                },
            )
        )
    if db.get(AppSetting, "account_data_ws") is None:
        db.add(
            AppSetting(
                key="account_data_ws",
                value={
                    "enabled": True,
                    "heartbeat_seconds": 20,
                    "reconnect_max_seconds": 60,
                    "writer_flush_seconds": 1,
                    "enrichment_refresh_seconds": 60,
                    "funding_refresh_seconds": 300,
                    "snapshot_stale_seconds": 90,
                },
            )
        )
    if db.get(AppSetting, "account_data_ws_state") is None:
        db.add(
            AppSetting(
                key="account_data_ws_state",
                value={
                    "connected": False,
                    "ready": False,
                    "active_subscriptions": 0,
                    "expected_subscriptions": 3,
                },
            )
        )
    leaderboard_alpha_defaults = {
        "enabled": True,
        "mode": "observe_only",
        "lookback_days": 30,
        "recent_signal_hours": 24,
        "min_traders_for_signal": 1,
        "min_quality_score": 55,
        "max_entry_lag_pct": 0.35,
        "sync_limit_per_run": 5,
        "auto_import_top_traders": True,
        "auto_import_limit": 20,
        "auto_import_window": "week",
        "auto_import_min_volume_usd": "0",
        "auto_import_refresh_hours": 4,
        "auto_sync_enabled": True,
        "auto_sync_interval_hours": 4,
        "include_auto_imported_in_signals": True,
        "copy_paper_enabled": True,
        "copy_paper_starting_equity_usd": "10000",
        "copy_paper_position_notional_usd": "100",
        "copy_paper_fee_rate": "0.00045",
        "copy_paper_min_source_notional_usd": "25",
        "copy_paper_max_open_positions": 80,
        "copy_paper_max_open_positions_per_trader": 10,
        "copy_paper_allowed_rank_limit": 20,
        "copy_paper_only_auto_top_traders": True,
        "copy_paper_close_on_opposite_open": True,
        "copy_paper_replay_batch_size": 1000,
        "copy_live_enabled": False,
        "copy_live_mode": "shadow",
        "copy_live_max_traders": 3,
        "copy_live_allowed_rank_limit": 20,
        "copy_live_allowed_classes": ["candidate"],
        "copy_live_min_quality_score": 80,
        "copy_live_min_copy_closed_positions": 20,
        "copy_live_min_profit_factor": "1.15",
        "copy_live_min_win_rate_pct": 40,
        "copy_live_min_avg_trade_pnl_usdc": "0",
        "copy_live_min_net_pnl_usdc": "0",
        "copy_live_recent_days": 7,
        "copy_live_min_recent_closed_positions": 2,
        "copy_live_position_notional_usd": "40",
        "copy_live_max_open_positions": 3,
        "copy_live_max_open_positions_per_trader": 1,
        "copy_live_max_estimated_notional_usd": "120",
        "copy_live_excluded_coin_prefixes": ["xyz:"],
        "copy_live_promotion_min_shortlist_days": 3,
        "copy_live_dry_run_requires_promotion": True,
    }
    leaderboard_alpha = db.get(AppSetting, "leaderboard_alpha")
    if leaderboard_alpha is None:
        db.add(
            AppSetting(
                key="leaderboard_alpha",
                value=leaderboard_alpha_defaults,
            )
        )
    elif isinstance(leaderboard_alpha.value, dict):
        merged = {**leaderboard_alpha_defaults, **leaderboard_alpha.value}
        if leaderboard_alpha.value.get("recent_signal_hours") == 6:
            merged["recent_signal_hours"] = 24
        if leaderboard_alpha.value.get("min_traders_for_signal") == 2:
            merged["min_traders_for_signal"] = 1
        if leaderboard_alpha.value.get("sync_limit_per_run") == 25:
            merged["sync_limit_per_run"] = 5
        leaderboard_alpha.value = merged
        db.add(leaderboard_alpha)
    if db.get(AppSetting, "leaderboard_alpha_state") is None:
        db.add(AppSetting(key="leaderboard_alpha_state", value={}))
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
                    "directional_penalty_lookback_trades": 200,
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
            if "directional_penalty_lookback_trades" not in updated:
                updated["directional_penalty_lookback_trades"] = 200
                mutated = True
            if "deep_defensive_cooldown_runs" not in updated:
                updated["deep_defensive_cooldown_runs"] = 6
                mutated = True
            if "deep_defensive_cooldown_min_new_trades" not in updated:
                updated["deep_defensive_cooldown_min_new_trades"] = 3
                mutated = True
            if mutated:
                existing.value = updated
    if db.get(AppSetting, "strategy_exclusions") is None:
        # Cull permanente delle strategie senza edge out-of-sample (Fase 0, giu 2026):
        # negative su PnL reale e profit factor sia in-sample sia OOS. `macd` escluso
        # anche per dedup — co-spara identico a `supertrend` (Jaccard 1.0) → doppio
        # voto trend nello scoring. Reversibile: togliere un nome riabilita la strategia
        # con storico intatto. Vedi backend/scripts/edge_oos_report.py.
        db.add(
            AppSetting(
                key="strategy_exclusions",
                value={
                    "strategies": [
                        "rsi",
                        "bollinger_bands",
                        "volume_profile",
                        "cvd_divergence",
                        "ob_imbalance",
                        "macd",
                    ]
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
                    "require_flow_confirmation": False,
                    "flow_lookback_hours": 6,
                    "flow_exchange": "BINANCE_FUTURES",
                    "flow_symbol": "BTCUSDT",
                    "flow_cache_ttl_seconds": 600,
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
                "require_flow_confirmation": False,
                "flow_lookback_hours": 6,
                "flow_exchange": "BINANCE_FUTURES",
                "flow_symbol": "BTCUSDT",
                "flow_cache_ttl_seconds": 600,
            }
            for key, default in reverse_defaults.items():
                if key not in updated:
                    updated[key] = default
                    mutated = True
            if mutated:
                existing.value = updated
    for timeframe in settings.enabled_timeframes:
        seed_timeframe(db, timeframe)
    seed_monitoring_markets(db)
    db.commit()


def seed_monitoring_markets(db: Session) -> None:
    """Inserisce (se assenti) i market di solo monitoraggio macro.

    Idempotente: non sovrascrive righe esistenti, così eventuali modifiche
    manuali (es. disattivazione) vengono preservate. `MarketSyncService` non
    tocca questi market perché non compaiono nella meta() standard di HL.
    """
    for entry in MONITORING_MARKETS:
        exists = (
            db.query(Market)
            .filter(
                Market.exchange == "hyperliquid",
                Market.symbol == entry["symbol"],
                Market.market_type == entry["market_type"],
            )
            .first()
        )
        if exists is None:
            db.add(
                Market(
                    exchange="hyperliquid",
                    symbol=entry["symbol"],
                    base=entry["base"],
                    quote=entry["quote"],
                    market_type=entry["market_type"],
                    active=True,
                    metadata_json=entry["metadata_json"],
                )
            )


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
                    enabled=strategy_name not in DEFAULT_DISABLED_STRATEGIES,
                    params=params,
                    weight=DEFAULT_STRATEGY_WEIGHTS[strategy_name],
                )
            )
        elif (
            strategy_name in LEGACY_DEFAULT_STRATEGY_WEIGHTS
            and exists.weight == LEGACY_DEFAULT_STRATEGY_WEIGHTS[strategy_name]
        ):
            exists.weight = DEFAULT_STRATEGY_WEIGHTS[strategy_name]
