from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction, OperationMode, OrderMode
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_operation_mode,
)
from app.db.models import AppSetting, Market, Position, Signal


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: list[str]


@dataclass(frozen=True)
class LiveOrderPlan:
    side: str
    size: Decimal
    notional: Decimal
    leverage: Decimal
    account_value: Decimal
    max_leverage: int = 10
    risk_amount: Decimal = Decimal("0")
    available_margin: Decimal | None = None


class OrderValidator:
    def validate_live_execution(
        self,
        db: Session,
        signal: Signal,
        settings: Settings,
        plan: LiveOrderPlan | None = None,
    ) -> ValidationResult:
        reasons: list[str] = []
        operation_mode = effective_operation_mode(db, settings)
        live_enabled = effective_live_trading_enabled(db, settings)
        live_settings = effective_live_execution_settings(db, settings)
        if operation_mode != OperationMode.AUTO_TRADE:
            reasons.append("operation mode is not AUTO_TRADE")
        if not live_enabled:
            reasons.append("ENABLE_LIVE_TRADING is false")
        if not settings.hyperliquid_account_address:
            reasons.append("HYPERLIQUID_ACCOUNT_ADDRESS is not configured")
        if not settings.hyperliquid_private_key:
            reasons.append("HYPERLIQUID_PRIVATE_KEY is not configured")
        if settings.hyperliquid_network == "mainnet" and not live_settings["enable_mainnet_live_trading"]:
            reasons.append("mainnet live trading requires enable_mainnet_live_trading")
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
            max_daily_loss_pct = float(risk_settings.value.get("max_daily_loss_pct", settings.max_daily_loss_pct))
            max_total_exposure_pct = float(
                risk_settings.value.get("max_total_exposure_pct", settings.max_total_exposure_pct)
            )
            max_leverage = float(risk_settings.value.get("max_leverage", settings.max_leverage))
        else:
            max_daily_loss_pct = settings.max_daily_loss_pct
            max_total_exposure_pct = settings.max_total_exposure_pct
            max_leverage = settings.max_leverage
        if signal.score < min_score:
            reasons.append("signal score below configured threshold")
        if signal.direction == Direction.NEUTRAL.value:
            reasons.append("neutral signals cannot be live executed")
        if signal.stop_loss is None:
            reasons.append("signal has no stop loss")
        market = db.get(Market, signal.market_id)
        if market is None:
            reasons.append("market not found")
        elif market.market_type != "perp":
            reasons.append("live execution currently supports perp markets only")
        duplicate = (
            db.query(Position)
            .filter(
                Position.market_id == signal.market_id,
                Position.status == "open",
                Position.mode == OrderMode.LIVE.value,
            )
            .first()
        )
        if duplicate is not None:
            reasons.append("live position already open for this market")
        open_positions = db.query(Position).filter(
            Position.status == "open",
            Position.mode == OrderMode.LIVE.value,
        ).count()
        if open_positions >= max_open_positions:
            reasons.append("max open positions reached")
        if signal.risk_reward is not None and float(signal.risk_reward) < min_risk_reward:
            reasons.append("risk/reward below configured threshold")
        if plan is None:
            return ValidationResult(ok=not reasons, reasons=reasons)
        if plan.size <= 0 or plan.notional <= 0:
            reasons.append("computed live order size is zero")
        if plan.account_value < Decimal(str(live_settings["min_account_value_usd"])):
            reasons.append("live account value below configured minimum")
        if plan.notional > Decimal(str(live_settings["order_notional_cap_usd"])):
            reasons.append("live order notional cap exceeded")
        min_notional = Decimal(str(live_settings["min_order_notional_usd"]))
        if plan.size > 0 and plan.notional < min_notional:
            reasons.append(
                f"live order notional below exchange minimum (${plan.notional:.2f} < ${min_notional:.2f})"
            )
        # Margine libero insufficiente: anche se il notional calcolato è ok,
        # HL non può fillare se account ha già tutto il margine impegnato.
        # Soglia: serve almeno min_notional / max_leverage di margine libero.
        # Check attivo solo se available_margin è stato passato esplicitamente.
        if plan.available_margin is not None:
            required_margin = min_notional / Decimal(str(max_leverage))
            if plan.available_margin < required_margin:
                reasons.append(
                    f"available margin insufficient "
                    f"(${plan.available_margin:.2f} < ${required_margin:.2f} required for min position)"
                )
        if plan.leverage > Decimal(str(max_leverage)):
            reasons.append("max leverage exceeded")
        current_risk = live_open_risk(db)
        total_risk = current_risk + plan.risk_amount
        max_risk = plan.account_value * Decimal(str(max_total_exposure_pct)) / Decimal("100")
        if total_risk > max_risk:
            reasons.append("max total exposure exceeded")
        daily_loss = live_daily_realized_loss(db)
        max_daily_loss = plan.account_value * Decimal(str(max_daily_loss_pct)) / Decimal("100")
        if daily_loss >= max_daily_loss:
            reasons.append("max daily loss reached")
        funding_data = (signal.strategy_breakdown or {}).get("_funding_rate")
        if isinstance(funding_data, dict):
            rate = funding_data.get("rate")
            if rate is not None:
                threshold_pct = float(
                    risk_settings.value.get("max_long_funding_rate_pct", settings.max_long_funding_rate_pct)
                    if risk_settings
                    else settings.max_long_funding_rate_pct
                )
                threshold = threshold_pct / 100
                is_long = signal.direction in {Direction.LONG.value, Direction.STRONG_LONG.value}
                is_short = signal.direction in {Direction.SHORT.value, Direction.STRONG_SHORT.value}
                if is_long and float(rate) > threshold:
                    reasons.append(
                        f"funding rate too high for long ({float(rate)*100:.4f}%/8h > {threshold_pct:.4f}%)"
                    )
                elif is_short and float(rate) < -threshold:
                    reasons.append(
                        f"funding rate too negative for short ({float(rate)*100:.4f}%/8h < -{threshold_pct:.4f}%)"
                    )
        return ValidationResult(ok=not reasons, reasons=reasons)


def live_open_risk(db: Session) -> Decimal:
    rows = (
        db.query(Position)
        .filter(Position.mode == OrderMode.LIVE.value, Position.status == "open")
        .all()
    )
    total = Decimal("0")
    for position in rows:
        if position.stop_loss is not None:
            total += abs(position.entry_price - position.stop_loss) * position.size
        else:
            total += position.entry_price * position.size
    return total


def live_daily_realized_loss(db: Session) -> Decimal:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = (
        db.query(func.coalesce(func.sum(Position.realized_pnl), 0))
        .filter(
            Position.mode == OrderMode.LIVE.value,
            Position.status == "closed",
            Position.closed_at >= start,
            Position.realized_pnl < 0,
        )
        .scalar()
    )
    return abs(Decimal(str(total or 0)))
