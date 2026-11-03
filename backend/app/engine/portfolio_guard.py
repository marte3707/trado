from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.runtime_settings import (
    DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE,
    effective_directional_reversal_breaker_settings,
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
        return self._new_market_probation_multiplier(db, signal)

    def evaluate_addition(
        self,
        db: Session,
        signal: Signal,
        position: Position,
        *,
        now: datetime | None = None,
    ) -> PortfolioGuardDecision:
        """Apply active portfolio breakers without counting a scale-in as a new position."""
        if position.status != "open" or position.mode != "live" or position.market_id != signal.market_id:
            return PortfolioGuardDecision(allowed=False, reason="addition target is not an open live position")
        checked_at = now or datetime.now(UTC)
        circuit = self._circuit_breaker_decision(db, checked_at)
        if not circuit.allowed:
            return circuit
        return self._directional_reversal_decision(
            db, signal, position.mode, position.side, checked_at
        )

    def _new_market_probation_multiplier(self, db: Session, signal: Signal) -> float:
        """Rischio ridotto sui mercati senza storico di trade chiusi.

        I mercati appena seedati tendono a bruciare stop pieni al primo segnale
        (coda di one-shot losers): sotto `min_closed_trades` posizioni chiuse il
        rischio per trade viene scalato di `size_multiplier`, finché il mercato
        non costruisce abbastanza storico per le market_score_adjustments.
        """
        config = setting_value(db, "risk").get("new_market_probation")
        if not isinstance(config, dict) or not bool(config.get("enabled", False)):
            return 1.0
        try:
            min_closed_trades = int(config.get("min_closed_trades", 3) or 0)
        except (TypeError, ValueError):
            min_closed_trades = 3
        if min_closed_trades <= 0:
            return 1.0
        size_multiplier = max(0.0, min(1.0, _float(config.get("size_multiplier"), 0.5)))
        if size_multiplier >= 1.0:
            return 1.0
        closed_count = (
            db.query(Position.id)
            .filter(
                Position.market_id == signal.market_id,
                Position.status == "closed",
            )
            .limit(min_closed_trades)
            .count()
        )
        return 1.0 if closed_count >= min_closed_trades else size_multiplier

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

        reversal_decision = self._directional_reversal_decision(db, signal, mode, side, now)
        if not reversal_decision.allowed:
            return reversal_decision

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
        low_timeframes = _low_tf_timeframes(config)
        same_low_tf = [
            position
            for position in same_direction
            if _position_timeframe(position) in low_timeframes
        ]
        open_r = sum((_open_r_units(position) for position in same_direction), Decimal("0"))
        new_r = Decimal("1")
        same_direction_markets = {
            int(position.market_id)
            for position in same_direction
            if position.market_id is not None
        }
        new_market_count = len(same_direction_markets | {int(signal.market_id)})
        new_this_cycle = _new_positions_this_cycle_by_side(
            db,
            open_positions,
            mode=mode_key,
            side=side_key,
            now=now,
        )
        details = {
            "mode": mode_key,
            "side": side_key,
            "signal_id": signal.id,
            "timeframe": signal.timeframe,
            "same_direction_open_positions": len(same_direction),
            "same_direction_open_markets": len(same_direction_markets),
            "same_direction_open_r": str(open_r),
            "same_direction_low_tf_open_positions": len(same_low_tf),
            "new_positions_this_cycle_by_side": new_this_cycle,
            "low_timeframes": sorted(low_timeframes),
        }

        max_new_per_cycle = _mode_number(
            config.get("max_new_positions_per_cycle_by_side"),
            mode_key,
        )
        if (
            max_new_per_cycle is not None
            and new_this_cycle is not None
            and new_this_cycle + 1 > int(max_new_per_cycle)
        ):
            return PortfolioGuardDecision(
                allowed=False,
                reason="max new directional positions per cycle reached",
                details={**details, "limit": int(max_new_per_cycle)},
            )

        max_positions = _mode_number(config.get("max_directional_open_positions"), mode_key)
        if max_positions is not None and len(same_direction) + 1 > int(max_positions):
            return PortfolioGuardDecision(
                allowed=False,
                reason="max directional open positions reached",
                details={**details, "limit": int(max_positions)},
            )

        max_markets = _mode_number(config.get("max_directional_markets"), mode_key)
        if max_markets is not None and new_market_count > int(max_markets):
            return PortfolioGuardDecision(
                allowed=False,
                reason="max directional markets reached",
                details={**details, "limit": int(max_markets), "new_market_count": new_market_count},
            )

        max_r = _mode_decimal(config.get("max_directional_risk_r"), mode_key)
        if max_r is not None and open_r + new_r > max_r:
            return PortfolioGuardDecision(
                allowed=False,
                reason="max directional open risk reached",
                details={**details, "limit_r": str(max_r), "new_position_r": str(new_r)},
            )

        if signal.timeframe in low_timeframes:
            max_low_tf = _mode_number(_low_tf_limit_config(config, mode_key), mode_key)
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
        directional_result = self.update_directional_reversal_breaker(db, now=now)
        config = effective_portfolio_circuit_breaker_settings(db)
        state_row = db.get(AppSetting, "portfolio_circuit_breaker_state")
        state = _merged_state(state_row.value if state_row is not None else {})

        if not bool(config.get("enabled", True)):
            if state.get("active_until") is not None:
                state["active_until"] = None
                state["reason"] = None
                _upsert_state(db, state_row, state)
            return {"status": "disabled", "state": state, "directional_reversal": directional_result}

        active_until = _parse_dt(state.get("active_until"))
        if active_until is not None and active_until > now:
            return {"status": "active", "state": state, "directional_reversal": directional_result}

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
            return {"status": "activated", "state": state, "directional_reversal": directional_result}

        _upsert_state(db, state_row, state)
        return {"status": "ok", "state": state, "directional_reversal": directional_result}

    def update_directional_reversal_breaker(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        config = effective_directional_reversal_breaker_settings(db)
        state_row = db.get(AppSetting, "portfolio_circuit_breaker_state")
        state = _merged_state(state_row.value if state_row is not None else {})
        directional_state = _directional_reversal_state(state)

        if not bool(config.get("enabled", True)):
            if directional_state:
                state["directional_reversal"] = {}
                _upsert_state(db, state_row, state)
            return {"status": "disabled", "active": {}}

        active_modes = _active_breaker_modes(config)
        if not active_modes:
            return {"status": "disabled", "active": directional_state}

        window_minutes = max(1, int(_float(config.get("window_minutes"), 120)))
        stop_loss_count = max(1, int(_float(config.get("stop_loss_count"), 4)))
        cooldown_minutes = max(1, int(_float(config.get("cooldown_minutes"), 180)))
        apply_timeframes = _string_set(config.get("apply_timeframes"))
        window_start = now - timedelta(minutes=window_minutes)
        positions = _closed_positions_since(db, window_start, now)
        triggered: list[str] = []

        counts: dict[str, int] = {}
        for position in positions:
            mode = str(position.mode or "").lower()
            side = str(position.side or "").lower()
            if mode not in active_modes or side not in {"long", "short"}:
                continue
            if apply_timeframes and _position_timeframe(position) not in apply_timeframes:
                continue
            if not _negative_stop_loss(position):
                continue
            key = _directional_reversal_key(mode, side)
            counts[key] = counts.get(key, 0) + 1

        for key, count in counts.items():
            entry = _state_entry(directional_state.get(key))
            active_until = _parse_dt(entry.get("active_until"))
            if count < stop_loss_count or (active_until is not None and active_until > now):
                entry.update(
                    {
                        "last_window_stop_losses": count,
                        "last_checked_at": now.isoformat(),
                        "last_window_start": window_start.isoformat(),
                    }
                )
                directional_state[key] = entry
                continue
            mode, side = key.split(":", 1)
            until = now + timedelta(minutes=cooldown_minutes)
            entry = {
                "active_until": until.isoformat(),
                "triggered_at": now.isoformat(),
                "mode": mode,
                "side": side,
                "reason": "same_side_stop_loss_cluster",
                "last_window_stop_losses": count,
                "last_window_start": window_start.isoformat(),
                "last_checked_at": now.isoformat(),
                "window_minutes": window_minutes,
                "stop_loss_count": stop_loss_count,
                "cooldown_minutes": cooldown_minutes,
                "apply_timeframes": sorted(apply_timeframes),
            }
            directional_state[key] = entry
            triggered.append(key)
            db.add(
                DecisionLog(
                    event_type="directional_reversal_breaker_activated",
                    message="Directional reversal breaker activated; blocking same-side low-TF entries",
                    data=entry,
                )
            )

        active_directional_state: dict[str, dict[str, Any]] = {}
        for key, raw_entry in list(directional_state.items()):
            entry = _state_entry(raw_entry)
            active_until = _parse_dt(entry.get("active_until"))
            if active_until is not None and active_until <= now:
                entry["expired_at"] = now.isoformat()
                directional_state[key] = entry
            elif active_until is not None:
                active_directional_state[key] = entry

        state["directional_reversal"] = directional_state
        _upsert_state(db, state_row, state)
        return {
            "status": "activated" if triggered else "ok",
            "triggered": triggered,
            "active": active_directional_state,
        }

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

    def _directional_reversal_decision(
        self,
        db: Session,
        signal: Signal,
        mode: str,
        side: str,
        now: datetime,
    ) -> PortfolioGuardDecision:
        config = effective_directional_reversal_breaker_settings(db)
        if not bool(config.get("enabled", True)):
            return PortfolioGuardDecision(allowed=True)
        mode_key = _mode_key(mode)
        side_key = str(side).lower()
        if mode_key not in _active_breaker_modes(config):
            return PortfolioGuardDecision(allowed=True)
        apply_timeframes = _string_set(config.get("apply_timeframes"))
        if apply_timeframes and signal.timeframe not in apply_timeframes:
            return PortfolioGuardDecision(allowed=True)
        state = _merged_state(setting_value(db, "portfolio_circuit_breaker_state"))
        directional_state = _directional_reversal_state(state)
        entry = _state_entry(directional_state.get(_directional_reversal_key(mode_key, side_key)))
        active_until = _parse_dt(entry.get("active_until"))
        if active_until is None or active_until <= now:
            return PortfolioGuardDecision(allowed=True)
        return PortfolioGuardDecision(
            allowed=False,
            reason="directional reversal breaker active",
            details={
                "mode": mode_key,
                "side": side_key,
                "signal_id": signal.id,
                "timeframe": signal.timeframe,
                "active_until": active_until.isoformat(),
                "triggered_at": entry.get("triggered_at"),
                "window_minutes": entry.get("window_minutes"),
                "stop_loss_count": entry.get("stop_loss_count"),
                "last_window_stop_losses": entry.get("last_window_stop_losses"),
                "apply_timeframes": entry.get("apply_timeframes"),
            },
        )

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


def _low_tf_timeframes(config: dict[str, Any]) -> set[str]:
    new_config = config.get("max_low_tf_directional_open_positions")
    if isinstance(new_config, dict):
        values = _string_set(new_config.get("apply_timeframes"))
        if values:
            return values
    legacy_config = config.get("max_low_tf_directional_positions")
    return _string_set(legacy_config.get("timeframes") if isinstance(legacy_config, dict) else [])


def _low_tf_limit_config(config: dict[str, Any], mode: str) -> Any:
    new_config = config.get("max_low_tf_directional_open_positions")
    if _mode_number(new_config, mode) is not None:
        return new_config
    return config.get("max_low_tf_directional_positions")


def _new_positions_this_cycle_by_side(
    db: Session,
    open_positions: list[Position],
    *,
    mode: str,
    side: str,
    now: datetime,
) -> int | None:
    cycle_started_at = _last_worker_cycle_at(db)
    if cycle_started_at is None:
        return None
    return sum(
        1
        for position in open_positions
        if str(position.side or "").lower() == side
        and str(position.mode or "").lower() == mode
        and _position_opened_between(position, cycle_started_at, now)
    )


def _last_worker_cycle_at(db: Session) -> datetime | None:
    try:
        row = (
            db.query(DecisionLog)
            .filter(DecisionLog.event_type == "worker_cycle")
            .order_by(DecisionLog.timestamp.desc())
            .first()
        )
    except Exception:
        return None
    if row is None:
        return None
    return _parse_dt(getattr(row, "timestamp", None))


def _position_opened_between(position: Position, start: datetime, end: datetime) -> bool:
    opened_at = _parse_dt(getattr(position, "opened_at", None))
    return opened_at is not None and start <= opened_at <= end


def _active_breaker_modes(config: dict[str, Any]) -> set[str]:
    modes = config.get("modes")
    if not isinstance(modes, dict):
        return {"live"}
    return {
        str(mode).lower()
        for mode, enabled in modes.items()
        if bool(enabled) and str(mode).lower() in {"paper", "live"}
    }


def _directional_reversal_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = state.get("directional_reversal")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): _state_entry(value)
        for key, value in raw.items()
        if isinstance(key, str)
    }


def _state_entry(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _directional_reversal_key(mode: str, side: str) -> str:
    return f"{mode}:{side}"


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
