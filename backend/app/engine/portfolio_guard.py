from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.runtime_settings import (
    DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE,
    effective_macro_risk_mode_settings,
    effective_portfolio_circuit_breaker_settings,
    effective_portfolio_guard_settings,
    setting_value,
)
from app.db.models import AppSetting, DecisionLog, Position, Signal


@dataclass(frozen=True)
class PortfolioGuardDecision:
    allowed: bool
    reason: str | None = None
    details: dict[str, Any] | None = None


class PortfolioGuard:
    """Portfolio-level pre-trade controls.

    This guard intentionally blocks only new entries. It never closes positions
    and it never replaces live OrderValidator checks.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def position_size_multiplier(self, db: Session, signal: Signal) -> float:
        macro = effective_macro_risk_mode_settings(db)
        if not bool(macro.get("enabled", False)):
            return 1.0
        multiplier = _float(macro.get("size_multiplier"), 1.0)
        return max(0.0, min(1.0, multiplier))

    def evaluate_entry(
        self,
        db: Session,
        signal: Signal,
        *,
        mode: str,
        side: str,
        now: datetime | None = None,
    ) -> PortfolioGuardDecision:
        now = now or datetime.now(UTC)

        circuit_decision = self._circuit_breaker_decision(db, now)
        if not circuit_decision.allowed:
            return circuit_decision

        macro_decision = self._macro_risk_mode_decision(db, signal)
        if not macro_decision.allowed:
            return macro_decision

        config = effective_portfolio_guard_settings(db)
        if not bool(config.get("enabled", True)):
            return PortfolioGuardDecision(allowed=True)

        mode_key = _mode_key(mode)
        side_key = str(side).lower()
        open_positions = _open_positions(db, mode_key)
        same_direction = [
            position
            for position in open_positions
            if str(position.side or "").lower() == side_key
        ]
        low_tf_config = config.get("max_low_tf_directional_positions")
        low_timeframes = _string_set(
            low_tf_config.get("timeframes") if isinstance(low_tf_config, dict) else []
        )
        same_low_tf = [
            position
            for position in same_direction
            if _position_timeframe(position) in low_timeframes
        ]
        open_r = sum((_open_r_units(position) for position in same_direction), Decimal("0"))
        new_r = Decimal("1")
        details = {
            "mode": mode_key,
            "side": side_key,
            "signal_id": signal.id,
            "timeframe": signal.timeframe,
            "same_direction_open_positions": len(same_direction),
            "same_direction_open_r": str(open_r),
            "same_direction_low_tf_open_positions": len(same_low_tf),
            "low_timeframes": sorted(low_timeframes),
        }

        max_positions = _mode_number(config.get("max_directional_open_positions"), mode_key)
        if max_positions is not None and len(same_direction) + 1 > int(max_positions):
            return PortfolioGuardDecision(
                allowed=False,
                reason="max directional open positions reached",
                details={**details, "limit": int(max_positions)},
            )

        max_r = _mode_decimal(config.get("max_directional_risk_r"), mode_key)
        if max_r is not None and open_r + new_r > max_r:
            return PortfolioGuardDecision(
                allowed=False,
                reason="max directional open risk reached",
                details={**details, "limit_r": str(max_r), "new_position_r": str(new_r)},
            )

        if signal.timeframe in low_timeframes:
            max_low_tf = _mode_number(low_tf_config, mode_key)
            if max_low_tf is not None and len(same_low_tf) + 1 > int(max_low_tf):
                return PortfolioGuardDecision(
                    allowed=False,
                    reason="max low-timeframe directional positions reached",
                    details={**details, "limit": int(max_low_tf)},
                )

        return PortfolioGuardDecision(allowed=True, details=details)

    def update_circuit_breaker(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        config = effective_portfolio_circuit_breaker_settings(db)
        state_row = db.get(AppSetting, "portfolio_circuit_breaker_state")
        state = _merged_state(state_row.value if state_row is not None else {})

        if not bool(config.get("enabled", True)):
            if state.get("active_until") is not None:
                state = _merged_state({"active_until": None, "reason": None})
                _upsert_state(db, state_row, state)
            return {"status": "disabled", "state": state}

        active_until = _parse_dt(state.get("active_until"))
        if active_until is not None and active_until > now:
            return {"status": "active", "state": state}

        if active_until is not None and active_until <= now:
            state["active_until"] = None
            state["reason"] = None
            _upsert_state(db, state_row, state)
            db.add(
                DecisionLog(
                    event_type="portfolio_circuit_breaker_expired",
                    message="Portfolio circuit breaker expired; new entries can resume",
                    data={"expired_at": now.isoformat()},
                )
            )

        window_minutes = max(1, int(_float(config.get("window_minutes"), 90)))
        window_start = now - timedelta(minutes=window_minutes)
        positions = _closed_positions_since(db, window_start, now)
        total_r = sum((_closed_r(position) for position in positions), Decimal("0"))
        stop_losses = sum(1 for position in positions if _negative_stop_loss(position))
        state["last_window_loss_r"] = str(total_r)
        state["last_window_stop_losses"] = stop_losses
        state["last_window_start"] = window_start.isoformat()
        state["last_checked_at"] = now.isoformat()

        max_loss_r = Decimal(str(_float(config.get("max_loss_r"), 5)))
        max_stop_losses = int(_float(config.get("max_stop_losses"), 8))
        triggers: list[str] = []
        if total_r <= -max_loss_r:
            triggers.append("window_loss_r")
        if max_stop_losses >= 0 and stop_losses >= max_stop_losses:
            triggers.append("stop_loss_count")

        if triggers:
            pause_minutes = max(1, int(_float(config.get("pause_minutes"), 180)))
            until = now + timedelta(minutes=pause_minutes)
            reason = ", ".join(triggers)
            state.update(
                {
                    "active_until": until.isoformat(),
                    "triggered_at": now.isoformat(),
                    "reason": reason,
                    "last_window_loss_r": str(total_r),
                    "last_window_stop_losses": stop_losses,
                }
            )
            _upsert_state(db, state_row, state)
            db.add(
                DecisionLog(
                    event_type="portfolio_circuit_breaker_activated",
                    message="Portfolio circuit breaker activated; blocking new entries",
                    data={
                        "reason": reason,
                        "active_until": until.isoformat(),
                        "window_minutes": window_minutes,
                        "window_loss_r": str(total_r),
                        "window_stop_losses": stop_losses,
                    },
                )
            )
            return {"status": "activated", "state": state}

        _upsert_state(db, state_row, state)
        return {"status": "ok", "state": state}

    def _circuit_breaker_decision(
        self,
        db: Session,
        now: datetime,
    ) -> PortfolioGuardDecision:
        config = effective_portfolio_circuit_breaker_settings(db)
        if not bool(config.get("enabled", True)):
            return PortfolioGuardDecision(allowed=True)
        state = _merged_state(setting_value(db, "portfolio_circuit_breaker_state"))
        active_until = _parse_dt(state.get("active_until"))
        if active_until is None or active_until <= now:
            return PortfolioGuardDecision(allowed=True)
        return PortfolioGuardDecision(
            allowed=False,
            reason="portfolio circuit breaker active",
            details={
                "active_until": active_until.isoformat(),
                "triggered_at": state.get("triggered_at"),
                "reason": state.get("reason"),
                "last_window_loss_r": state.get("last_window_loss_r"),
                "last_window_stop_losses": state.get("last_window_stop_losses"),
            },
        )

    def _macro_risk_mode_decision(
        self,
        db: Session,
        signal: Signal,
    ) -> PortfolioGuardDecision:
        config = effective_macro_risk_mode_settings(db)
        if not bool(config.get("enabled", False)):
            return PortfolioGuardDecision(allowed=True)
        disabled_timeframes = _string_set(config.get("disable_timeframes"))
        details = {
            "signal_id": signal.id,
            "timeframe": signal.timeframe,
            "score": signal.score,
        }
        if signal.timeframe in disabled_timeframes:
            return PortfolioGuardDecision(
                allowed=False,
                reason="macro risk mode disabled this timeframe",
                details={**details, "disabled_timeframes": sorted(disabled_timeframes)},
            )

        risk_value = setting_value(db, "risk")
        min_score = int(_float(risk_value.get("min_signal_score"), self.settings.min_signal_score))
        min_score_add = int(_float(config.get("min_score_add"), 0))
        required_score = min_score + min_score_add
        if min_score_add > 0 and signal.score < required_score:
            return PortfolioGuardDecision(
                allowed=False,
                reason="macro risk mode score threshold not met",
                details={**details, "required_score": required_score},
            )

        if bool(config.get("require_mtf_confluence", False)):
            breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
            if signal.timeframe in {"5m", "15m", "30m", "1h"} and "_mtf_confluence" not in breakdown:
                return PortfolioGuardDecision(
                    allowed=False,
                    reason="macro risk mode requires MTF confluence",
                    details=details,
                )

        return PortfolioGuardDecision(allowed=True, details=details)


def _mode_key(mode: str) -> str:
    raw = getattr(mode, "value", mode)
    return str(raw).lower()


def _open_positions(db: Session, mode: str) -> list[Position]:
    rows = (
        db.query(Position)
        .filter(Position.mode == mode, Position.status == "open")
        .all()
    )
    return [
        row
        for row in rows
        if row.mode == mode and row.status == "open"
    ]


def _closed_positions_since(db: Session, start: datetime, end: datetime) -> list[Position]:
    rows = (
        db.query(Position)
        .filter(Position.closed_at.is_not(None), Position.closed_at >= start)
        .all()
    )
    return [
        row
        for row in rows
        if row.closed_at is not None
        and start <= _ensure_aware(row.closed_at) <= end
        and row.realized_pnl is not None
    ]


def _position_timeframe(position: Position) -> str:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    value = metadata.get("source_timeframe")
    return value if isinstance(value, str) else "unknown"


def _open_r_units(position: Position) -> Decimal:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    initial_size = _decimal(metadata.get("initial_size"))
    if initial_size is None or initial_size <= 0:
        return Decimal("1")
    try:
        remaining = Decimal(str(position.size or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("1")
    if remaining <= 0:
        return Decimal("0")
    units = remaining / initial_size
    if units < 0:
        return Decimal("0")
    return min(units, Decimal("1"))


def _closed_r(position: Position) -> Decimal:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    risk = _decimal(metadata.get("initial_position_risk"))
    if risk is None or risk <= 0 or position.realized_pnl is None:
        return Decimal("0")
    try:
        return Decimal(str(position.realized_pnl)) / risk
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return Decimal("0")


def _negative_stop_loss(position: Position) -> bool:
    metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
    reason = metadata.get("exit_reason") or metadata.get("last_exit_reason")
    if reason != "stop_loss" or position.realized_pnl is None:
        return False
    try:
        return Decimal(str(position.realized_pnl)) <= 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _mode_number(value: Any, mode: str) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(mode)
    if raw is None:
        return None
    number = _float(raw, -1)
    return number if number >= 0 else None


def _mode_decimal(value: Any, mode: str) -> Decimal | None:
    number = _mode_number(value, mode)
    if number is None:
        return None
    return Decimal(str(number))


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _ensure_aware(parsed)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merged_state(value: Any) -> dict[str, Any]:
    state = dict(DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE)
    if isinstance(value, dict):
        state.update(value)
    return state


def _upsert_state(
    db: Session,
    state_row: AppSetting | None,
    state: dict[str, Any],
) -> None:
    if state_row is None:
        state_row = AppSetting(key="portfolio_circuit_breaker_state", value=state)
    else:
        state_row.value = dict(state)
    db.add(state_row)
