from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import Direction
from app.core.runtime_settings import (
    DEFAULT_PORTFOLIO_CIRCUIT_BREAKER_STATE,
    effective_directional_reversal_breaker_settings,
    effective_macro_risk_mode_settings,
    effective_portfolio_circuit_breaker_settings,
    effective_portfolio_guard_settings,
    setting_value,
)
from app.db.models import AppSetting, Candle, DecisionLog, Position, Signal
from app.engine.mtf import check_mtf_confluence, higher_timeframe
from app.strategies.base import CandleInput


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
        multiplier = 1.0
        macro = _effective_macro_policy(db)
        if macro["active"]:
            macro_multiplier = _float(macro["policy"].get("size_multiplier"), 1.0)
            multiplier *= max(0.0, min(1.0, macro_multiplier))
        multiplier *= self._new_market_probation_multiplier(db, signal)
        return multiplier

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

    def evaluate_macro_risk_gate(
        self,
        db: Session,
        signal: Signal,
    ) -> PortfolioGuardDecision:
        """Macro-only gate shared by execution and public signal publishing."""
        return self._macro_risk_mode_decision(db, signal)

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

    def _macro_risk_mode_decision(
        self,
        db: Session,
        signal: Signal,
    ) -> PortfolioGuardDecision:
        macro = _effective_macro_policy(db)
        if not macro["active"]:
            return PortfolioGuardDecision(allowed=True)
        policy = macro["policy"]
        disabled_timeframes = _string_set(policy.get("disable_timeframes"))
        details = {
            "signal_id": signal.id,
            "timeframe": signal.timeframe,
            "score": signal.score,
            "macro_level": macro.get("level"),
            "macro_sources": macro.get("sources"),
            "macro_reason": macro.get("reason"),
        }
        if signal.timeframe in disabled_timeframes:
            return PortfolioGuardDecision(
                allowed=False,
                reason="macro risk mode disabled this timeframe",
                details={**details, "disabled_timeframes": sorted(disabled_timeframes)},
            )

        if bool(policy.get("require_mtf_confluence", False)):
            mtf_passed, mtf_details = _evaluate_macro_mtf_confluence(db, signal)
            if not mtf_passed:
                return PortfolioGuardDecision(
                    allowed=False,
                    reason="macro risk mode requires MTF confluence",
                    details={**details, **mtf_details},
                )

        expected_r_floor = _optional_float(policy.get("min_expected_r"))
        if expected_r_floor is not None:
            breakdown = signal.strategy_breakdown if isinstance(signal.strategy_breakdown, dict) else {}
            raw_calibration = breakdown.get("_expected_r_calibration")
            calibration: dict[str, Any] = raw_calibration if isinstance(raw_calibration, dict) else {}
            confidence = str(calibration.get("confidence") or "").lower()
            expected_r = _optional_float(calibration.get("expected_r"))
            if confidence in {"medium", "high"} and expected_r is not None and expected_r < expected_r_floor:
                return PortfolioGuardDecision(
                    allowed=False,
                    reason="macro risk mode expected R threshold not met",
                    details={
                        **details,
                        "required_expected_r": expected_r_floor,
                        "calibrated_expected_r": expected_r,
                        "calibration_confidence": confidence,
                        "calibration_samples": calibration.get("n_samples"),
                    },
                )

        return PortfolioGuardDecision(allowed=True, details=details)


def _mode_key(mode: str) -> str:
    raw = getattr(mode, "value", mode)
    return str(raw).lower()


def _effective_macro_policy(db: Session) -> dict[str, Any]:
    manual = effective_macro_risk_mode_settings(db)
    state = setting_value(db, "macro_risk_state")
    policies: list[dict[str, Any]] = []
    sources: list[str] = []
    level = "off"
    reason = None

    if bool(manual.get("enabled", False)):
        policies.append(manual)
        sources.append("manual")
        level = "manual"
        reason = "manual_macro_risk_mode"

    if _macro_state_active(state):
        policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
        if policy:
            policies.append(policy)
            sources.extend(str(item) for item in state.get("sources", []) if item)
            level = str(state.get("level") or level)
            reason = state.get("reason") or reason

    if not policies:
        return {"active": False, "policy": {}, "level": "off", "sources": [], "reason": None}

    merged = {
        "size_multiplier": min((_float(policy.get("size_multiplier"), 1.0) for policy in policies), default=1.0),
        "disable_timeframes": sorted(
            {
                timeframe
                for policy in policies
                for timeframe in _string_set(policy.get("disable_timeframes"))
            }
        ),
        "require_mtf_confluence": _macro_mtf_required(
            manual=manual,
            state=state,
        ),
    }
    expected_r_floors = [
        floor
        for floor in (_optional_float(policy.get("min_expected_r")) for policy in policies)
        if floor is not None
    ]
    merged["min_expected_r"] = max(expected_r_floors) if expected_r_floors else None
    return {
        "active": True,
        "policy": merged,
        "level": level,
        "sources": sorted(set(sources)),
        "reason": reason,
    }


def _macro_state_active(state: dict[str, Any]) -> bool:
    if not bool(state.get("active", False)):
        return False
    active_until = _parse_dt(state.get("active_until"))
    return not (active_until is not None and active_until <= datetime.now(UTC))


def _macro_mtf_required(*, manual: dict[str, Any], state: dict[str, Any]) -> bool:
    manual_required = bool(
        manual.get("enabled", False) and manual.get("require_mtf_confluence", False)
    )
    if manual_required:
        return True
    if not _macro_state_active(state):
        return False
    raw_policy = state.get("policy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    return bool(
        str(state.get("level") or "").lower() == "extreme"
        and policy.get("require_mtf_confluence", False)
    )


def _evaluate_macro_mtf_confluence(
    db: Session,
    signal: Signal,
) -> tuple[bool, dict[str, Any]]:
    if signal.timeframe not in {"5m", "15m", "30m", "1h"}:
        return True, {}
    higher_tf = higher_timeframe(signal.timeframe)
    if higher_tf is None:
        return True, {}
    direction = _signal_direction(signal.direction)
    risk_value = setting_value(db, "risk")
    mtf_ema_period = int(risk_value.get("mtf_ema_period", 50))
    higher_rows = (
        db.query(Candle)
        .filter(Candle.market_id == signal.market_id, Candle.timeframe == higher_tf)
        .order_by(Candle.open_time.desc())
        .limit(max(mtf_ema_period * 3, 100))
        .all()
    )
    higher_candles = [
        CandleInput(
            open_time=row.open_time,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            open_interest=float(row.open_interest) if row.open_interest is not None else None,
        )
        for row in reversed(higher_rows)
    ]
    mtf_passed, mtf_info = check_mtf_confluence(
        higher_candles,
        direction,
        enabled=True,
        ema_period=mtf_ema_period,
    )
    if mtf_passed or mtf_info is None:
        return True, {}
    return False, {
        "higher_timeframe": higher_tf,
        "mtf": mtf_info,
    }


def _signal_direction(value: Any) -> Direction:
    try:
        return Direction(str(value))
    except ValueError:
        return Direction.NEUTRAL


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


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


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
