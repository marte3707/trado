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
    hyperliquid_rate_limit_cooldown_until: float = 0.0
    last_rate_limit_log_at: float = 0.0


def _record_guard_subcycle(
    db,
    cycle_id: str,
    label: str,
    result: Any,
    expected_interval_seconds: int,
    *,
    ok_message: str,
) -> None:
    """Aggiorna lo stato del sotto-ciclo guard guidato dal fast_risk loop.

    Quando fast_risk possiede liquidation guard e UAR al posto del loop
    principale, i rispettivi `cycle_id` (live_liquidation_guard_cycle /
    unified_account_ratio_guard_cycle) altrimenti non verrebbero mai aggiornati
    e le card del cruscotto finirebbero 'stale'. Registriamo qui solo
    sull'esecuzione riuscita e committiamo subito (le azioni si auto-committano
    internamente): un fallimento transitorio successivo nello stesso ciclo non
    deve annullare questo record. I fallimenti 429/circuit-breaker restano
    aggregati in `fast_risk_cycle` e non sporcano la salute del singolo guard.
    """
    status = (
        result["status"]
        if isinstance(result, dict) and isinstance(result.get("status"), str)
        else "ok"
    )
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


def _compact_error(exc: Exception, *, max_length: int = 240) -> str:
    message = str(exc).replace("\n", " ").strip()
    if not message:
        message = type(exc).__name__
    if len(message) > max_length:
        return f"{message[:max_length]}..."
    return message


def _is_hyperliquid_rate_limit_error(error: str) -> bool:
    lower = error.lower()
    return "429" in lower or "rate limit" in lower or "too many requests" in lower


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
            errors: dict[str, str] = {}
            if current_time < state.hyperliquid_rate_limit_cooldown_until:
                remaining = int(state.hyperliquid_rate_limit_cooldown_until - current_time)
                result: dict[str, Any] = {
                    "status": "skipped",
                    "reason": "hyperliquid_rate_limit_cooldown",
                    "cooldown_remaining_seconds": remaining,
                    "actions": actions,
                    "duration_seconds": round(time.monotonic() - started, 4),
                    "warn": False,
                }
                _record_system_cycle_status(
                    db,
                    "fast_risk_cycle",
                    "Fast Risk Cycle",
                    "skipped",
                    message="Fast risk cycle skipped during Hyperliquid rate-limit cooldown",
                    data=result,
                    expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
                )
                if current_time - state.last_rate_limit_log_at >= 300:
                    db.add(
                        DecisionLog(
                            event_type="fast_risk_rate_limit_cooldown",
                            message="Fast risk cycle skipped during Hyperliquid rate-limit cooldown",
                            data=result,
                        )
                    )
                    state.last_rate_limit_log_at = current_time
                db.commit()
                logger.warning("fast_risk_rate_limit_cooldown", **result)
                return result

            # Ogni azione ha il proprio try/except: un 429 (o altro fallimento
            # transitorio) su una singola azione non deve piu' far contare le
            # altre due come fallite (vedi diagnosi 16/06/2026 sui falsi
            # fast_risk_cycle_failed). Le 3 funzioni-azione si auto-committano
            # internamente in caso di successo, quindi il rollback locale qui
            # annulla solo il lavoro non committato della azione fallita.
            liquidation_due = (
                current_time - state.last_liquidation_at
                >= settings.fast_risk_liquidation_interval_seconds
            )
            if liquidation_due:
                try:
                    liquidation = await asyncio.to_thread(
                        executor.position_manager.audit_live_positions_against_liquidation,
                        db,
                    )
                    actions["liquidation_guard"] = liquidation
                    state.last_liquidation_at = current_time
                    _record_guard_subcycle(
                        db,
                        "live_liquidation_guard_cycle",
                        "Live Liquidation Guard Cycle",
                        liquidation,
                        settings.fast_risk_liquidation_interval_seconds,
                        ok_message="Live liquidation guard cycle completed",
                    )
                except Exception as exc:
                    db.rollback()
                    state.last_liquidation_at = current_time
                    error = _compact_error(exc)
                    errors["liquidation_guard"] = error
                    logger.exception("fast_risk_liquidation_guard_failed", error=error)

            reconcile_due = (
                current_time - state.last_reconcile_at
                >= settings.fast_risk_reconcile_interval_seconds
            )
            if reconcile_due:
                try:
                    actions["live_reconciled"] = await asyncio.to_thread(
                        reconcile_live_positions,
                        db,
                    )
                    state.last_reconcile_at = current_time
                except Exception as exc:
                    db.rollback()
                    state.last_reconcile_at = current_time
                    error = _compact_error(exc)
                    errors["live_reconciled"] = error
                    logger.exception("fast_risk_reconcile_failed", error=error)

            uar_due = current_time - state.last_uar_at >= settings.fast_risk_uar_interval_seconds
            if uar_due:
                try:
                    uar_result = await asyncio.to_thread(
                        audit_unified_account_ratio_guard,
                        db,
                        executor,
                    )
                    actions["unified_account_ratio"] = uar_result
                    state.last_uar_at = current_time
                    _record_guard_subcycle(
                        db,
                        "unified_account_ratio_guard_cycle",
                        "Unified Account Ratio Guard Cycle",
                        uar_result,
                        settings.fast_risk_uar_interval_seconds,
                        ok_message="Unified Account Ratio guard cycle completed",
                    )
                except Exception as exc:
                    db.rollback()
                    state.last_uar_at = current_time
                    error = _compact_error(exc)
                    errors["unified_account_ratio"] = error
                    logger.exception("fast_risk_uar_guard_failed", error=error)

            duration_seconds = time.monotonic() - started
            status = "failed" if errors else "ok"
            result: dict[str, Any] = {
                "status": status,
                "duration_seconds": round(duration_seconds, 4),
                "warn": duration_seconds > settings.fast_risk_warn_after_seconds,
                "actions": actions,
            }
            if errors:
                result["errors"] = errors
                if any(_is_hyperliquid_rate_limit_error(error) for error in errors.values()):
                    state.hyperliquid_rate_limit_cooldown_until = current_time + max(
                        60,
                        int(settings.fast_risk_rate_limit_cooldown_seconds),
                    )
                    result["rate_limit_cooldown_seconds"] = int(
                        state.hyperliquid_rate_limit_cooldown_until - current_time
                    )
            _record_system_cycle_status(
                db,
                "fast_risk_cycle",
                "Fast Risk Cycle",
                status,
                message="Fast risk cycle completed" if not errors else "Fast risk cycle completed with errors",
                data=result,
                expected_interval_seconds=settings.fast_risk_loop_interval_seconds,
            )
            if errors:
                db.add(
                    DecisionLog(
                        event_type="fast_risk_cycle_failed",
                        message="Fast risk cycle: one or more actions failed",
                        data=result,
                    )
                )
            db.commit()
            if errors:
                logger.warning("fast_risk_cycle_completed_with_errors", **result)
            else:
                logger.info("fast_risk_cycle_completed", **result)
            return result
        except Exception as exc:
            duration_seconds = time.monotonic() - started
            db.rollback()
            data = {
                "status": "failed",
                "error": _compact_error(exc),
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
            logger.exception("fast_risk_cycle_failed", error=data["error"])
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
