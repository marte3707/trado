"""Stato dei cicli di sistema (system_cycle_status) e heartbeat del worker."""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.db.models import AppSetting, DecisionLog
from app.db.session import SessionLocal

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.

logger = structlog.get_logger()
SYSTEM_CYCLE_STATUS_SETTING_KEY = "system_cycle_status"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _record_system_cycle_status(
    db,
    cycle_id: str,
    label: str,
    status: str,
    *,
    message: str,
    data: dict[str, Any] | None = None,
    expected_interval_seconds: int | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    setting = db.get(AppSetting, SYSTEM_CYCLE_STATUS_SETTING_KEY)
    value = dict(setting.value) if setting is not None and isinstance(setting.value, dict) else {}
    cycles = dict(value.get("cycles", {})) if isinstance(value.get("cycles"), dict) else {}
    previous = dict(cycles.get(cycle_id, {})) if isinstance(cycles.get(cycle_id), dict) else {}
    previous_failures = previous.get("consecutive_failures", 0)
    try:
        consecutive_failures = int(previous_failures)
    except (TypeError, ValueError):
        consecutive_failures = 0
    if status == "failed":
        consecutive_failures += 1
    elif status in {"ok", "skipped", "disabled"}:
        consecutive_failures = 0

    cycles[cycle_id] = {
        "id": cycle_id,
        "label": label,
        "status": status,
        "message": message,
        "data": _json_safe(data or {}),
        "last_seen_at": now,
        "last_ok_at": now if status == "ok" else previous.get("last_ok_at"),
        "last_failure_at": now if status == "failed" else previous.get("last_failure_at"),
        "consecutive_failures": consecutive_failures,
        "expected_interval_seconds": expected_interval_seconds,
    }
    value["updated_at"] = now
    value["cycles"] = cycles
    if setting is None:
        setting = AppSetting(key=SYSTEM_CYCLE_STATUS_SETTING_KEY, value=value)
    else:
        setting.value = value
    db.add(setting)


def _record_heartbeat(phase: str) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(UTC).isoformat()
        setting = db.get(AppSetting, "worker_heartbeat")
        if setting is None:
            db.add(AppSetting(key="worker_heartbeat", value={"timestamp": now, "phase": phase}))
        else:
            setting.value = {"timestamp": now, "phase": phase}
        db.commit()
    except Exception:
        logger.exception("worker_heartbeat_write_failed")
    finally:
        db.close()


def _record_worker_failure(exc: Exception) -> None:
    db = SessionLocal()
    try:
        db.add(
            DecisionLog(
                event_type="worker_cycle_failed",
                message="Worker cycle raised an exception",
                data={"error": str(exc), "type": type(exc).__name__},
            )
        )
        db.commit()
    except Exception:
        logger.exception("worker_failure_log_failed")
    finally:
        db.close()


