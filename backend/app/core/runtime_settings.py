from typing import Any, cast

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OperationMode
from app.db.models import AppSetting


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
    }


def effective_alert_min_score(db: Session, settings: Settings) -> int:
    value = setting_value(db, "alerts").get("min_score")
    if value is None:
        return settings.min_signal_score
    try:
        return int(value)
    except (TypeError, ValueError):
        return settings.min_signal_score


def _float(value: object | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return default
