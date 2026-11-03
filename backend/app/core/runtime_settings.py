from typing import Any, cast

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting

DEFAULT_POSITION_MANAGEMENT_SETTINGS: dict[str, Any] = {
    "timeStop": {
        "enabled": True,
        "paperEnabled": True,
        # Una posizione che supera la durata massima senza aver raggiunto TP1
        # non deve restare aperta indefinitamente solo perche' e' in perdita.
        # I trade profittevoli e i runner restano invece affidati al trailing.
        "closeLosingPaperPositions": True,
        "closeProfitablePaperPositions": False,
        "preserveTakeProfitRunners": True,
        "liveMode": "alert",
        "maxAgeHoursByTimeframe": {
            "5m": 12,
            "15m": 24,
            "30m": 48,
            "1h": 96,
            "4h": 192,
            "1d": 336,
        },
    },
    "staleMarketData": {
        "enabled": True,
        # Solo paper: il live non viene mai chiuso usando un mark locale.
        "paperMode": "close",
        "freshMarkMaxAgeMinutes": 5,
        "alertCooldownMinutes": 360,
        # Margine ampio rispetto alla durata della candela: evita chiusure per
        # un singolo ciclo perso, ma impedisce posizioni non piu' gestibili.
        "maxCandleAgeHoursByTimeframe": {
            "1m": 0.5,
            "5m": 1,
            "15m": 2,
            "30m": 4,
            "1h": 8,
            "4h": 24,
            "1d": 72,
        },
    },
    "trailingStop": {
        "enabled": True,
        "activationR": 1.0,
        "mode": "hybrid",
        "atrPeriod": 14,
        "atrMultiplierInitial": 3.0,
        "atrMultiplierAfterTp1": 2.5,
        "atrMultiplierAfterTp2": 2.0,
        "useChandelierExit": True,
        "useSwingTrailing": True,
        "useEmaTrailing": True,
        "emaPeriod": 21,
        "swingLookback": 20,
        "swingBufferAtr": 0.5,
        "emaBufferAtr": 0.5,
        "minStopUpdateDistancePct": 0.15,
        "updateOnlyOnCandleClose": True,
        # Timeframe di riferimento usato per la cadenza di ri-valutazione del
        # trailing (e come fonte prezzo intraday). Per i timeframe lenti lo stop
        # viene controllato sulla chiusura di una candela piu' fine, mantenendo
        # pero' ATR/EMA (la larghezza del trail) sul timeframe della posizione.
        "checkIntervalByTimeframe": {
            "1d": "1h",
            "4h": "1h",
            "1h": "15m",
            "30m": "15m",
            "15m": "5m",
            "5m": "5m",
            "1m": "1m",
        },
        "moveToBreakevenAtR": 1.0,
        "lockProfitAtR": 2.0,
        "lockProfitStopR": 1.0,
    },
    "preTpProfitLock": {
        "enabled": True,
        "activationR": 0.5,
        "minProfitUsd": 1.5,
        "lockFractionOfR": 0.35,
        "minLockR": 0.15,
        "maxLockR": 0.35,
    },
    "livePortfolioProfitLock": {
        "enabled": True,
        "activationUnrealizedPnlUsd": 10.0,
        "givebackPct": 35.0,
        "minPositionProfitUsd": 1.5,
        "marketCloseEnabled": False,
    },
    "takeProfit": {
        "enabled": True,
        "tp1R": 1.0,
        "tp1ClosePct": 30,
        "tp2R": 2.0,
        "tp2ClosePct": 30,
        "runnerPct": 40,
        "runnerMode": "trailing_stop",
    },
}


DEFAULT_THE_BULL_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "channel_username": "thebull_crypto",
    # The public Telegram preview is paged. Keep enough posts to cover the
    # expected worker interval without treating an old backlog as a new call.
    "feed_history_limit": 25,
    # Source-specific opt-in. Global operation mode and all live guardrails
    # remain mandatory when this is switched to ``live``.
    "execution_mode": "paper",
    "timeframe": "4h",
    "max_signal_age_minutes": 60,
    "signal_score": 60,
    "risk_multiplier": 0.5,
    "max_open_positions": 3,
    # The Bull scale-ins are bounded against the original filled asset size.
    # Never bump an undersized addition up to the exchange minimum.
    "max_additions_per_ticker": 2,
    "max_addition_fraction": 0.30,
    "min_stop_distance_pct": 0.03,
    "close_on_profit_update": True,
    "close_companion_paper_on_source_exit": True,
    # Broader source language is retained as evidence and the explicitly
    # supported subset can be routed through the normal execution path.
    "semantic_audit_enabled": True,
    # Supported source calls enter the normal guarded execution path.
    "expanded_entry_execution": True,
    # Chart review uses the macOS Vision framework locally. A zone is eligible
    # only when the current market midpoint is inside the OCR-derived range;
    # the normal risk plan and all global execution guardrails still apply.
    "chart_analysis_enabled": True,
    "chart_zone_execution": True,
    # A chart whose range cannot be read must not discard an otherwise
    # identifiable The Bull call. This remains an explicit runtime opt-in and
    # never overrides a successfully read zone that says the current price is
    # outside the intended range.
    "chart_ocr_fallback_execution": False,
    "chart_analysis_max_per_cycle": 2,
    # A separate opt-in for The Bull-only spot routing.  It never enables spot
    # execution for native strategies and all normal live guards still apply.
    "spot_fallback_enabled": False,
}

DEFAULT_PORTFOLIO_GUARD_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "max_new_positions_per_cycle_by_side": {
        "live": 3,
    },
    "max_directional_open_positions": {
        "paper": 25,
        "live": 13,
    },
    "max_directional_markets": {
        "live": 10,
    },
    "max_directional_risk_r": {
        "paper": 25,
        "live": 8,
    },
    "max_low_tf_directional_positions": {
        "timeframes": ["5m", "15m"],
        "paper": 25,
    },
    "max_low_tf_directional_open_positions": {
        "apply_timeframes": ["5m", "15m"],
        "live": 8,
    },
}

DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "window_minutes": 90,
    "max_loss_r": 5,
    "max_stop_losses": 8,
    "pause_minutes": 180,
    "scope": "new_entries_only",
}

DEFAULT_DIRECTIONAL_REVERSAL_BREAKER_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "modes": {
        "paper": False,
        "live": True,
    },
    "apply_timeframes": ["5m", "15m"],
    "window_minutes": 120,
    "stop_loss_count": 4,
    "cooldown_minutes": 180,
}

DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE: dict[str, Any] = {
    "active_until": None,
    "triggered_at": None,
    "reason": None,
    "last_window_loss_r": 0,
    "last_window_stop_losses": 0,
    "directional_reversal": {},
}


def setting_value(db: Session, key: str) -> dict:
    row = db.get(AppSetting, key)
    return row.value if row is not None and isinstance(row.value, dict) else {}


def effective_operation_mode(db: Session, settings: Settings) -> OperationMode:
    value = setting_value(db, "operation_mode").get("mode")
    try:
        return OperationMode(str(value))
    except ValueError:
        return settings.operation_mode


def effective_live_trading_enabled(db: Session, settings: Settings) -> bool:
    operation = setting_value(db, "operation_mode")
    if "enable_live_trading" in operation:
        return bool(operation["enable_live_trading"])
    live = setting_value(db, "live_trading")
    if "enabled" in live:
        return bool(live["enabled"])
    return settings.enable_live_trading


def effective_enabled_timeframes(db: Session, settings: Settings) -> list[str]:
    values = _normalized_timeframe_list(setting_value(db, "timeframes").get("enabled"))
    if values:
        return values
    return _normalized_timeframe_list(settings.enabled_timeframes)


def effective_strategy_exclusions(db: Session) -> set[str]:
    """Strategie cullate permanentemente (sorgente unica di verità).

    Escluse dalla generazione segnali (SignalEngine) e mai riabilitate
    dall'auto-retest della calibrazione. Popolata dalla Fase 0 edge analysis:
    strategie senza edge out-of-sample. Reversibile: togliere un nome dalla
    lista la riabilita, con storico intatto.
    """
    raw = setting_value(db, "strategy_exclusions").get("strategies")
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if isinstance(item, str) and item}


def effective_monitoring_only_timeframes(db: Session) -> list[str]:
    timeframes = setting_value(db, "timeframes")
    enabled = set(_normalized_timeframe_list(timeframes.get("enabled")))
    monitoring_only = _normalized_timeframe_list(timeframes.get("monitoring_only"))
    return [timeframe for timeframe in monitoring_only if timeframe not in enabled]


def effective_paper_equity(db: Session, settings: Settings) -> float:
    value = setting_value(db, "paper").get("equity")
    if value is None:
        return settings.paper_equity
    try:
        return float(value)
    except (TypeError, ValueError):
        return settings.paper_equity


def effective_live_execution_settings(db: Session, settings: Settings) -> dict:
    value = setting_value(db, "live_execution")
    return {
        "new_live_entries_enabled": bool(value.get("new_live_entries_enabled", True)),
        "readiness_gate_enabled": bool(value.get("readiness_gate_enabled", False)),
        "readiness_lookback_trades": int(_float(value.get("readiness_lookback_trades"), 200.0)),
        "readiness_recent_trades": int(_float(value.get("readiness_recent_trades"), 50.0)),
        "readiness_min_closed_trades": int(_float(value.get("readiness_min_closed_trades"), 100.0)),
        "readiness_min_recent_trades": int(_float(value.get("readiness_min_recent_trades"), 30.0)),
        "readiness_min_profit_factor": _float(value.get("readiness_min_profit_factor"), 1.2),
        "readiness_min_recent_profit_factor": _float(
            value.get("readiness_min_recent_profit_factor"), 1.1
        ),
        "readiness_min_avg_net_r": _float(value.get("readiness_min_avg_net_r"), 0.05),
        "readiness_min_recent_avg_net_r": _float(
            value.get("readiness_min_recent_avg_net_r"), 0.02
        ),
        "readiness_max_drawdown_r": _float(value.get("readiness_max_drawdown_r"), 10.0),
        "min_account_value_usd": _float(
            value.get("min_account_value_usd"),
            settings.live_min_account_value_usd,
        ),
        "order_notional_cap_usd": _float(
            value.get("order_notional_cap_usd"),
            settings.live_order_notional_cap_usd,
        ),
        "min_order_notional_usd": _float(
            value.get("min_order_notional_usd"),
            settings.live_min_order_notional_usd,
        ),
        "min_reduce_only_notional_usd": _float(
            value.get("min_reduce_only_notional_usd"),
            10.0,
        ),
        "bump_to_min_notional": bool(
            value.get("bump_to_min_notional", settings.live_bump_to_min_notional)
        ),
        "max_slippage_pct": _float(value.get("max_slippage_pct"), settings.live_max_slippage_pct),
        "order_expires_after_seconds": int(
            _float(
                value.get("order_expires_after_seconds"),
                float(settings.live_order_expires_after_seconds),
            )
        ),
        "enable_mainnet_live_trading": bool(
            value.get("enable_mainnet_live_trading", settings.enable_mainnet_live_trading)
        ),
        "close_companion_paper_on_live_reconcile": bool(
            value.get("close_companion_paper_on_live_reconcile", False)
        ),
    }


def effective_new_live_entries_enabled(db: Session, settings: Settings) -> bool:
    """Entry-only live guard.

    This is intentionally separate from ``enable_live_trading``: turning it off
    must not disable reconciliation, protective orders, stops, take profits, or
    operator-initiated reduce-only closes for positions already open.
    """
    return bool(effective_live_execution_settings(db, settings)["new_live_entries_enabled"])


def effective_position_management_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "position_management")
    return _deep_merge(DEFAULT_POSITION_MANAGEMENT_SETTINGS, value)


def effective_portfolio_guard_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "risk").get("portfolio_guard")
    overrides = value if isinstance(value, dict) else {}
    return _deep_merge(DEFAULT_PORTFOLIO_GUARD_SETTINGS, overrides)


def effective_portfolio_circuit_breaker_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "risk").get("circuit_breaker")
    overrides = value if isinstance(value, dict) else {}
    return _deep_merge(DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_SETTINGS, overrides)


def effective_directional_reversal_breaker_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "risk").get("directional_reversal_breaker")
    overrides = value if isinstance(value, dict) else {}
    return _deep_merge(DEFAULT_DIRECTIONAL_REVERSAL_BREAKER_SETTINGS, overrides)


def effective_alert_min_score(db: Session, settings: Settings) -> int:
    value = setting_value(db, "alerts").get("min_score")
    if value is None:
        return settings.min_signal_score
    try:
        return int(value)
    except (TypeError, ValueError):
        return settings.min_signal_score


def effective_telegram_notifications_enabled(db: Session) -> bool:
    """Return the runtime kill switch for outbound Telegram notifications.

    Missing settings preserve the legacy enabled behaviour; an explicit false
    value is fail-closed and must be honoured by every worker notification path.
    """
    value = setting_value(db, "alerts").get("telegram_enabled")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


MIN_SIGNAL_EXPECTED_R_BOUNDS = (-0.5, 1.0)


def effective_min_signal_expected_r(db: Session) -> float | None:
    """Soglia minima di Expected R-multiple calibrato per gate del signal engine.

    Letta da `AppSetting("risk").value["min_signal_expected_r"]`. `None` = gate disabilitato.
    Valori fuori da [-0.5, 1.0] vengono ignorati (gate disabilitato) per evitare freeze totale.
    """
    raw = setting_value(db, "risk").get("min_signal_expected_r")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    low, high = MIN_SIGNAL_EXPECTED_R_BOUNDS
    if value < low or value > high:
        return None
    return value


def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, default_value in defaults.items():
        override_value = overrides.get(key)
        if isinstance(default_value, dict) and isinstance(override_value, dict):
            result[key] = _deep_merge(default_value, override_value)
        elif override_value is not None:
            result[key] = override_value
        else:
            result[key] = default_value.copy() if isinstance(default_value, dict) else default_value
    for key, override_value in overrides.items():
        if key not in result:
            result[key] = override_value
    return result


def _float(value: object | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _normalized_timeframe_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        values.append(value)
        seen.add(value)
    return values
