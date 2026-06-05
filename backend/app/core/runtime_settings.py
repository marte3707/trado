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
    raw = setting_value(db, "timeframes").get("enabled")
    if isinstance(raw, list):
        values = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
        if values:
            return values
    return settings.enabled_timeframes


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
