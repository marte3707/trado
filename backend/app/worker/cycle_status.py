"""Stato dei cicli di sistema (system_cycle_status) e heartbeat del worker."""
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.db.models import AppSetting, DecisionLog
from app.db.session import SessionLocal

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


async def run_periodic_cycle(
    *,
    cycle_id: str,
    label: str,
    expected_interval_seconds: int,
    work: Callable[[Any], Awaitable[dict[str, Any] | None]],
    ok_message: str,
    failed_message: str,
    ok_status: Callable[[dict[str, Any] | None], str] | None = None,
) -> None:
    """Esegue un job periodico del worker con sessione, stato ciclo e logging standard.

    Incapsula il boilerplate ripetuto (sessione dedicata, registrazione dello stato
    del ciclo, rollback + DecisionLog su errore) che prima era copiato per ogni
    guardrail nel loop principale di `worker/main.py`. `work` riceve la sessione e
    ritorna un dict di risultato (loggato e salvato come `data`) oppure `None`.
    """
    db = SessionLocal()
    try:
        result = await work(db)
        status = ok_status(result) if ok_status is not None else "ok"
        _record_system_cycle_status(
            db,
            cycle_id,
            label,
            status,
            message=ok_message,
            data=result if isinstance(result, dict) else None,
            expected_interval_seconds=expected_interval_seconds,
        )
        db.commit()
        if isinstance(result, dict):
            logger.info(f"{cycle_id}_completed", **result)
    except Exception as exc:
        logger.exception(f"{cycle_id}_failed", error=str(exc))
        db.rollback()
        try:
            data = {"error": str(exc), "type": type(exc).__name__}
            _record_system_cycle_status(
                db,
                cycle_id,
                label,
                "failed",
                message=failed_message,
                data=data,
                expected_interval_seconds=expected_interval_seconds,
            )
            db.add(
                DecisionLog(
                    event_type=f"{cycle_id}_failed",
                    message=failed_message,
                    data=data,
                )
            )
            db.commit()
        except Exception:
            logger.exception(f"{cycle_id}_failure_log_failed")
    finally:
        db.close()


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


