"""Trigger e orchestrazione di auto-calibration e auto-tuner dal worker."""
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.enums import OrderMode
from app.db.models import AppSetting, DecisionLog, Position
from app.engine.auto_tuner import AutoTuner
from app.engine.calibration import AutoCalibrationEngine

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.

logger = structlog.get_logger()


def closed_paper_trade_count(db) -> int:
    return (
        db.query(Position)
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "closed",
            Position.realized_pnl.is_not(None),
        )
        .count()
    )


def calibration_min_closed_trades(db) -> int:
    setting = db.get(AppSetting, "auto_calibration")
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    try:
        return int(value.get("min_closed_trades", 20))
    except (TypeError, ValueError):
        return 20


def last_calibration_trade_count(db) -> int | None:
    state = db.get(AppSetting, "auto_calibration_state")
    value = state.value if state is not None and isinstance(state.value, dict) else {}
    # `last_trade_count` is the calibration lookback sample size and caps at
    # `lookback_trades`; the worker needs the unbounded closed-trade total.
    for key in ("last_total_closed_trade_count", "last_trade_count"):
        try:
            return int(value[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _auto_calibration_state(db) -> dict[str, Any]:
    state = db.get(AppSetting, "auto_calibration_state")
    return dict(state.value) if state is not None and isinstance(state.value, dict) else {}


def _state_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _state_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _starvation_soft_threshold(db) -> int:
    setting = db.get(AppSetting, "auto_calibration")
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    try:
        return int(value.get("starvation_soft_cycles", 10))
    except (TypeError, ValueError):
        return 10


def _starvation_hard_threshold(db) -> int:
    setting = db.get(AppSetting, "auto_calibration")
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    try:
        return int(value.get("starvation_hard_cycles", 15))
    except (TypeError, ValueError):
        return 15


def _consecutive_zero_signal_cycles_from_log(db) -> int:
    # Include sia "worker_cycle" (con generated=0) sia "worker_cycle_failed":
    # un crash del cycle non genera segnali ed è semanticamente equivalente a
    # uno zero-cycle ai fini dello starvation override.
    # I tick "no-op" (active_timeframes vuoto) sono saltati: il worker non ha
    # nemmeno provato a generare segnali, quindi non rappresentano vera starvation.
    recent = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
        .order_by(DecisionLog.timestamp.desc())
        .limit(120)
        .all()
    )
    streak = 0
    for entry in recent:
        if entry.event_type == "worker_cycle_failed":
            streak += 1
            continue
        if not isinstance(entry.data, dict):
            break
        active_tf = entry.data.get("active_timeframes")
        if isinstance(active_tf, list) and not active_tf:
            continue
        if int(entry.data.get("generated", 0)) == 0:
            streak += 1
        else:
            break
    return streak


def _last_active_worker_cycle(db) -> DecisionLog | None:
    recent = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
        .order_by(DecisionLog.timestamp.desc())
        .limit(120)
        .all()
    )
    for entry in recent:
        if entry.event_type == "worker_cycle_failed":
            return entry
        if not isinstance(entry.data, dict):
            return entry
        active_tf = entry.data.get("active_timeframes")
        if isinstance(active_tf, list) and not active_tf:
            continue
        return entry
    return None


def _worker_cycle_generated(entry: DecisionLog) -> bool:
    if entry.event_type == "worker_cycle_failed" or not isinstance(entry.data, dict):
        return False
    return _state_int(entry.data.get("generated"), 0) > 0


def _starvation_calibration_due(db, zero_cycles: int) -> bool:
    state = _auto_calibration_state(db)

    if bool(state.get("starvation_override_active")):
        last_active_cycle = _last_active_worker_cycle(db)
        if last_active_cycle is None or not _worker_cycle_generated(last_active_cycle):
            return False
        last_run_at = _state_datetime(state.get("last_run_at"))
        cycle_at = _state_datetime(last_active_cycle.timestamp)
        return last_run_at is None or (cycle_at is not None and cycle_at > last_run_at)

    if not bool(state.get("deep_defensive_active")):
        return False

    previous_zero_cycles = _state_int(state.get("consecutive_zero_signal_cycles"), 0)
    soft_threshold = _starvation_soft_threshold(db)
    hard_threshold = _starvation_hard_threshold(db)
    return (
        previous_zero_cycles < soft_threshold <= zero_cycles
        or previous_zero_cycles < hard_threshold <= zero_cycles
    )


def _record_calibration_total_closed_trade_count(db, total_closed: int) -> None:
    state = db.get(AppSetting, "auto_calibration_state") or AppSetting(
        key="auto_calibration_state",
        value={},
    )
    value = dict(state.value) if isinstance(state.value, dict) else {}
    value["last_total_closed_trade_count"] = total_closed
    state.value = value
    db.add(state)
    db.commit()


def should_run_calibration(db, closed_this_cycle: int) -> bool:
    if closed_this_cycle > 0:
        return True
    total_closed = closed_paper_trade_count(db)
    if total_closed < calibration_min_closed_trades(db):
        return False
    last_count = last_calibration_trade_count(db)
    if last_count is None or last_count != total_closed:
        return True
    # Trigger anche senza nuovi trade se la starvation è attiva: la calibrazione
    # deve poter aggiornare consecutive_zero_signal_cycles e applicare l'attenuazione.
    return _starvation_calibration_due(
        db,
        _consecutive_zero_signal_cycles_from_log(db),
    )


def run_calibration(db, calibration_engine: AutoCalibrationEngine) -> bool:
    try:
        total_closed = closed_paper_trade_count(db)
        result = calibration_engine.run(db)
        _record_calibration_total_closed_trade_count(db, total_closed)
        return result.eligible
    except Exception as exc:
        db.rollback()
        db.add(
            DecisionLog(
                event_type="auto_calibration_failed",
                message="Auto calibration failed",
                data={"error": str(exc)},
            )
        )
        db.commit()
        logger.exception("auto_calibration_failed", error=str(exc))
        return False


def run_auto_tuner(db) -> dict:
    try:
        return AutoTuner().run(db)
    except Exception as exc:
        db.rollback()
        db.add(
            DecisionLog(
                event_type="auto_tuner_failed",
                message="Auto tuner failed",
                data={"error": str(exc)},
            )
        )
        db.commit()
        logger.exception("auto_tuner_failed", error=str(exc))
        return {"status": "error", "error": str(exc)}


