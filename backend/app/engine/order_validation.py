from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import OperationMode, OrderMode
from app.core.runtime_settings import effective_live_trading_enabled, effective_operation_mode
from app.db.models import AppSetting, Position, Signal


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: list[str]


class OrderValidator:
    def validate_live_execution(self, db: Session, signal: Signal, settings: Settings) -> ValidationResult:
        reasons: list[str] = []
        operation_mode = effective_operation_mode(db, settings)
        live_enabled = effective_live_trading_enabled(db, settings)
        if operation_mode != OperationMode.AUTO_TRADE:
            reasons.append("operation mode is not AUTO_TRADE")
        if not live_enabled:
            reasons.append("ENABLE_LIVE_TRADING is false")
        kill_switch = db.get(AppSetting, "kill_switch")
        if kill_switch and kill_switch.value.get("enabled"):
            reasons.append("kill switch enabled")
        risk_settings = db.get(AppSetting, "risk")
        min_score = settings.min_signal_score
        min_risk_reward = settings.min_risk_reward
        max_open_positions = settings.max_open_positions
        if risk_settings:
            min_score = int(risk_settings.value.get("min_signal_score", min_score))
            min_risk_reward = float(risk_settings.value.get("min_risk_reward", min_risk_reward))
            max_open_positions = int(risk_settings.value.get("max_open_positions", max_open_positions))
        if signal.score < min_score:
            reasons.append("signal score below configured threshold")
        open_positions = db.query(Position).filter(
            Position.status == "open",
            Position.mode == OrderMode.LIVE.value,
        ).count()
        if open_positions >= max_open_positions:
            reasons.append("max open positions reached")
        if signal.risk_reward is not None and float(signal.risk_reward) < min_risk_reward:
            reasons.append("risk/reward below configured threshold")
        return ValidationResult(ok=not reasons, reasons=reasons)
