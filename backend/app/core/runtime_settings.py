from typing import Any, cast

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting

DEFAULT_POSITION_MANAGEMENT_SETTINGS: dict[str, Any] = {
    "timeStop": {
        "enabled": True,
        "paperEnabled": True,
        "closeLosingPaperPositions": False,
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
        "moveToBreakevenAtR": 1.0,
        "lockProfitAtR": 2.0,
        "lockProfitStopR": 1.0,
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

DEFAULT_PORTFOLIO_GUARD_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "max_directional_open_positions": {
        "paper": 20,
        "live": 12,
    },
    "max_directional_risk_r": {
        "paper": 12,
        "live": 6,
    },
    "max_low_tf_directional_positions": {
        "timeframes": ["5m", "15m"],
        "paper": 12,
        "live": 6,
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

DEFAULT_MACRO_RISK_MODE_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "size_multiplier": 0.5,
    "min_score_add": 0,
    "min_expected_r": 0.0,
    "disable_timeframes": ["5m"],
    "require_mtf_confluence": True,
}

DEFAULT_MACRO_RISK_MONITOR_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "actions_enabled": True,
    "telegram_notify_cooldown_minutes": 180,
    "telegram_notify_on_downgrade": False,
    "news_enabled": True,
    "calendar_enabled": True,
    "market_stress_enabled": True,
    "news_feed_urls": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ],
    "calendar_feed_urls": [],
    "news_fetch_cooldown_minutes": 15,
    "calendar_fetch_cooldown_hours": 6,
    "news_lookback_hours": 12,
    "calendar_lookahead_hours": 48,
    "calendar_pre_event_minutes": 90,
    "calendar_post_event_minutes": 45,
    "market_lookback_minutes": 120,
    "watch_score": 40,
    "high_score": 60,
    "extreme_score": 80,
    "level_hysteresis_points": 5,
    "calendar_static_events": [
        {
            "key": "fomc_2026_06",
            "title": "FOMC rate decision and projections",
            "category": "macro_calendar",
            "source": "Federal Reserve",
            "starts_at": "2026-06-17T14:00:00-04:00",
            "score": 72,
            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        },
        {
            "key": "fomc_2026_07",
            "title": "FOMC rate decision",
            "category": "macro_calendar",
            "source": "Federal Reserve",
            "starts_at": "2026-07-29T14:00:00-04:00",
            "score": 70,
            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        },
        {
            "key": "fomc_2026_09",
            "title": "FOMC rate decision and projections",
            "category": "macro_calendar",
            "source": "Federal Reserve",
            "starts_at": "2026-09-16T14:00:00-04:00",
            "score": 72,
            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        },
        {
            "key": "fomc_2026_10",
            "title": "FOMC rate decision",
            "category": "macro_calendar",
            "source": "Federal Reserve",
            "starts_at": "2026-10-28T14:00:00-04:00",
            "score": 70,
            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        },
        {
            "key": "fomc_2026_12",
            "title": "FOMC rate decision and projections",
            "category": "macro_calendar",
            "source": "Federal Reserve",
            "starts_at": "2026-12-09T14:00:00-05:00",
            "score": 72,
            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        },
        {
            "key": "bea_pce_2026_06",
            "title": "Personal Income and Outlays / PCE inflation",
            "category": "macro_calendar",
            "source": "BEA",
            "starts_at": "2026-06-25T08:30:00-04:00",
            "score": 66,
            "url": "https://www.bea.gov/news/schedule",
        },
        {
            "key": "bea_gdp_2026_q2_advance",
            "title": "GDP advance estimate",
            "category": "macro_calendar",
            "source": "BEA",
            "starts_at": "2026-07-30T08:30:00-04:00",
            "score": 62,
            "url": "https://www.bea.gov/news/schedule",
        },
        {
            "key": "treasury_quarterly_refunding",
            "title": "Treasury quarterly refunding window",
            "category": "liquidity",
            "source": "U.S. Treasury",
            "starts_at": "2026-07-29T08:30:00-04:00",
            "score": 55,
            "url": "https://home.treasury.gov/policy-issues/financing-the-government/quarterly-refunding",
        },
    ],
    "ttl_minutes_by_level": {
        "watch": 120,
        "high": 360,
        "extreme": 720,
    },
    "policies": {
        "watch": {
            "size_multiplier": 1.0,
            "min_score_add": 0,
            "min_expected_r": None,
            "disable_timeframes": [],
            "require_mtf_confluence": True,
        },
        "high": {
            "size_multiplier": 0.5,
            "min_score_add": 0,
            "min_expected_r": 0.0,
            "disable_timeframes": ["5m"],
            "require_mtf_confluence": True,
        },
        "extreme": {
            "size_multiplier": 0.25,
            "min_score_add": 0,
            "min_expected_r": 0.0,
            "disable_timeframes": ["5m"],
            "require_mtf_confluence": True,
        },
    },
}

DEFAULT_MACRO_RISK_STATE: dict[str, Any] = {
    "active": False,
    "level": "off",
    "reason": None,
    "confidence": 0,
    "combined_score": 0,
    "news_score": 0,
    "calendar_score": 0,
    "market_stress_score": 0,
    "triggered_at": None,
    "active_until": None,
    "last_checked_at": None,
    "summary": None,
    "sources": [],
    "policy": {},
    "top_news": [],
    "calendar": {},
    "market_stress": {},
    "last_news_fetch_at": None,
    "last_calendar_fetch_at": None,
    "cached_news_items": [],
    "cached_calendar_events": [],
    "last_telegram_risk_notification_at": None,
    "last_telegram_risk_notification_level": None,
}

DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE: dict[str, Any] = {
    "active_until": None,
    "triggered_at": None,
    "reason": None,
    "last_window_loss_r": 0,
    "last_window_stop_losses": 0,
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


def effective_macro_risk_mode_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "risk").get("macro_risk_mode")
    overrides = value if isinstance(value, dict) else {}
    return _deep_merge(DEFAULT_MACRO_RISK_MODE_SETTINGS, overrides)


def effective_macro_risk_monitor_settings(db: Session) -> dict[str, Any]:
    value = setting_value(db, "risk").get("macro_risk_monitor")
    overrides = value if isinstance(value, dict) else {}
    return _deep_merge(DEFAULT_MACRO_RISK_MONITOR_SETTINGS, overrides)


def effective_alert_min_score(db: Session, settings: Settings) -> int:
    value = setting_value(db, "alerts").get("min_score")
    if value is None:
        return settings.min_signal_score
    try:
        return int(value)
    except (TypeError, ValueError):
        return settings.min_signal_score


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
