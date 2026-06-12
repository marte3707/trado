"""Fast risk loop: reconcile live, liquidation guard e UAR su cadenza rapida."""
import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.enums import OperationMode
from app.core.runtime_settings import (
    effective_live_trading_enabled,
    effective_operation_mode,
)
from app.db.models import AppSetting, DecisionLog
from app.db.session import SessionLocal
from app.engine.execution import OrderExecutionEngine
from app.services.live_reconciliation import reconcile_live_positions

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.
from app.worker.cycle_status import (
    SYSTEM_CYCLE_STATUS_SETTING_KEY,
    _record_system_cycle_status,
)
from app.worker.uar_guard import audit_unified_account_ratio_guard

logger = structlog.get_logger()
_FAST_RISK_LOCK = asyncio.Lock()


class _FastRiskState:
    last_liquidation_at: float = 0.0
    last_reconcile_at: float = 0.0
    last_uar_at: float = 0.0
    consecutive_failures: int = 0

async def run_fast_risk_once(
    executor: OrderExecutionEngine,
    state: _FastRiskState | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Run a lightweight live-only risk pass outside the main signal cycle."""
    settings = executor.settings
    state = state or _FastRiskState()
    started = time.monotonic()
    current_time = time.time() if now is None else now
    if _FAST_RISK_LOCK.locked():
        return {"status": "skipped", "reason": "overlap"}

    async with _FAST_RISK_LOCK:
        db = SessionLocal()
        try:
            if not settings.fast_risk_loop_enabled:
                disabled_result: dict[str, Any] = {"status": "skipped", "reason": "disabled"}
                _record_system_cycle_status(
                    db,
                    "fast_risk_cycle",
                    "Fast Risk Cycle",
                    "skipped",
                    message="Fast risk cycle skipped",
                    data=disabled_result,
                    expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
                )
                db.commit()
                return disabled_result
            operation_mode = effective_operation_mode(db, settings)
            live_enabled = effective_live_trading_enabled(db, settings)
            if operation_mode != OperationMode.AUTO_TRADE or not live_enabled:
                live_skipped_result: dict[str, Any] = {
                    "status": "skipped",
                    "reason": "live_not_enabled",
                    "operation_mode": operation_mode.value,
                    "live_enabled": live_enabled,
                }
                _record_system_cycle_status(
                    db,
                    "fast_risk_cycle",
                    "Fast Risk Cycle",
                    "skipped",
                    message="Fast risk cycle skipped",
                    data=live_skipped_result,
                    expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
                )
                db.commit()
                return live_skipped_result

            actions: dict[str, Any] = {}
            liquidation_due = (
                current_time - state.last_liquidation_at
                >= settings.fast_risk_liquidation_interval_seconds
            )
            if liquidation_due:
                liquidation = await asyncio.to_thread(
                    executor.position_manager.audit_live_positions_against_liquidation,
                    db,
                )
                actions["liquidation_guard"] = liquidation
                state.last_liquidation_at = current_time

            reconcile_due = (
                current_time - state.last_reconcile_at
                >= settings.fast_risk_reconcile_interval_seconds
            )
            if reconcile_due:
                actions["live_reconciled"] = await asyncio.to_thread(
                    reconcile_live_positions,
                    db,
                )
                state.last_reconcile_at = current_time

            uar_due = current_time - state.last_uar_at >= settings.fast_risk_uar_interval_seconds
            if uar_due:
                actions["unified_account_ratio"] = await asyncio.to_thread(
                    audit_unified_account_ratio_guard,
                    db,
                    executor,
                )
                state.last_uar_at = current_time

            duration_seconds = time.monotonic() - started
            ok_result: dict[str, Any] = {
                "status": "ok",
                "duration_seconds": round(duration_seconds, 4),
                "warn": duration_seconds > settings.fast_risk_warn_after_seconds,
                "actions": actions,
            }
            _record_system_cycle_status(
                db,
                "fast_risk_cycle",
                "Fast Risk Cycle",
                "ok",
                message="Fast risk cycle completed",
                data=ok_result,
                expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
            )
            db.commit()
            logger.info("fast_risk_cycle_completed", **ok_result)
            return ok_result
        except Exception as exc:
            duration_seconds = time.monotonic() - started
            db.rollback()
            data = {
                "status": "failed",
                "error": str(exc),
                "type": type(exc).__name__,
                "duration_seconds": round(duration_seconds, 4),
            }
            try:
                _record_system_cycle_status(
                    db,
                    "fast_risk_cycle",
                    "Fast Risk Cycle",
                    "failed",
                    message="Fast risk cycle failed",
                    data=data,
                    expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
                )
                db.add(
                    DecisionLog(
                        event_type="fast_risk_cycle_failed",
                        message="Fast risk cycle failed",
                        data=data,
                    )
                )
                db.commit()
            except Exception:
                logger.exception("fast_risk_cycle_failure_log_failed")
            logger.exception("fast_risk_cycle_failed", error=str(exc))
            return data
        finally:
            db.close()


async def run_fast_risk_loop(
    executor: OrderExecutionEngine,
    state: _FastRiskState | None = None,
) -> None:
    state = state or _FastRiskState()
    settings = executor.settings
    while True:
        result = await run_fast_risk_once(executor, state)
        if result.get("status") == "failed":
            state.consecutive_failures += 1
            # Exponential backoff on consecutive failures (e.g. 429 rate-limit):
            # 15s → 30s → 60s → 120s → 300s max. This lets CloudFront reset
            # instead of hammering the API indefinitely.
            sleep_seconds = min(
                settings.fast_risk_loop_interval_seconds * (2 ** min(state.consecutive_failures - 1, 4)),
                300,
            )
        else:
            state.consecutive_failures = 0
            sleep_seconds = max(1, int(settings.fast_risk_loop_interval_seconds))
        await asyncio.sleep(sleep_seconds)


def _last_cycle_ok_at(db, cycle_id: str) -> float | None:
    """Restituisce l'epoch dell'ultima esecuzione riuscita di un ciclo, o None se mai girato."""
    setting = db.get(AppSetting, SYSTEM_CYCLE_STATUS_SETTING_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return None
    entry = setting.value.get("cycles", {}).get(cycle_id, {})
    raw = entry.get("last_ok_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


