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
    try:
        return float(value)
    except (TypeError, ValueError):
        return settings.paper_equity
