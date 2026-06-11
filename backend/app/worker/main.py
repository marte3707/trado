import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import OperationMode, OrderMode, SignalStatus
from app.core.logging import configure_logging
from app.core.runtime_settings import (
    effective_alert_min_score,
    effective_enabled_timeframes,
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_operation_mode,
)
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.models import AppSetting, DecisionLog, Market, Position, Signal
from app.db.session import SessionLocal
from app.engine.auto_tuner import AutoTuner
from app.engine.btc_macro_protection import BtcMacroProtectionEngine
from app.engine.calibration import AutoCalibrationEngine
from app.engine.execution import OrderExecutionEngine
from app.engine.macro_risk_monitor import MacroRiskMonitor
from app.engine.signal_engine import SignalEngine
from app.notifications.diagnostic_report import DiagnosticReporter
from app.notifications.telegram import TelegramNotifier
from app.services.candle_sync import TIMEFRAME_MS, CandleSyncService
from app.services.data_retention import prune_old_data
from app.services.hyperliquid import (
    HyperliquidTradingClient,
    HyperliquidTransientError,
    fetch_perp_open_interests,
)
from app.services.live_reconciliation import reconcile_live_positions
from app.services.market_sync import MarketSyncService
from app.services.position_pnl import refresh_open_position_pnls
from app.services.unified_account_ratio import (
    DEFAULT_UAR_RISK_SETTINGS,
    calculate_unified_account_ratio,
    decimal_or_none,
    free_spot_usdc_from_balances,
)
from app.worker.scheduler import (
    DATA_RETENTION_INTERVAL_SECONDS,
    DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
    LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
    UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
    PeriodicScheduler,
    next_daily_recap_timestamp,
)

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.
__all__ = ["next_daily_recap_timestamp"]

logger = structlog.get_logger()
_LAST_TIMEFRAME_RUN_MS: dict[str, int] = {}
_TICK_LOCK = asyncio.Lock()
_FAST_RISK_LOCK = asyncio.Lock()
SYSTEM_CYCLE_STATUS_SETTING_KEY = "system_cycle_status"


@dataclasses.dataclass
class _WorkerStats:
    cycles: int = 0
    signals_generated: int = 0
    candle_errors: int = 0
    cycle_failures: int = 0
    period_start: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class _FastRiskState:
    last_liquidation_at: float = 0.0
    last_reconcile_at: float = 0.0
    last_uar_at: float = 0.0


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


def should_run_timeframe(timeframe: str, now_ms: int) -> bool:
    interval_ms = TIMEFRAME_MS[timeframe]
    return now_ms % interval_ms < 60_000


def active_timeframes_for_tick(timeframes: list[str], now_ms: int) -> list[str]:
    active: list[str] = []
    for timeframe in timeframes:
        if timeframe not in TIMEFRAME_MS:
            continue
        interval_ms = TIMEFRAME_MS[timeframe]
        last_run_ms = _LAST_TIMEFRAME_RUN_MS.get(timeframe, 0)
        if should_run_timeframe(timeframe, now_ms) or now_ms - last_run_ms >= interval_ms:
            active.append(timeframe)
    return active


def mark_timeframes_run(timeframes: list[str], started_ms: int) -> None:
    for timeframe in timeframes:
        _LAST_TIMEFRAME_RUN_MS[timeframe] = started_ms


def paper_management_timeframes(db, enabled_timeframes: list[str]) -> list[str]:
    """Keep paper positions manageable even after timeframe config changes."""
    managed: list[str] = []
    seen: set[str] = set()

    for timeframe in enabled_timeframes:
        if timeframe in TIMEFRAME_MS and timeframe not in seen:
            managed.append(timeframe)
            seen.add(timeframe)

    positions = (
        db.query(Position)
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "open",
        )
        .all()
    )
    discovered: set[str] = set()
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_timeframe = metadata.get("source_timeframe")
        if not isinstance(source_timeframe, str) or not source_timeframe:
            source_signal_id = metadata.get("source_signal_id", 0)
            try:
                signal_id = int(source_signal_id)
            except (TypeError, ValueError):
                signal_id = None
            if signal_id is not None:
                signal = db.get(Signal, signal_id)
                if signal is not None and isinstance(signal.timeframe, str):
                    source_timeframe = signal.timeframe
        if isinstance(source_timeframe, str) and source_timeframe in TIMEFRAME_MS:
            discovered.add(source_timeframe)

    for timeframe in sorted(discovered, key=lambda item: TIMEFRAME_MS[item]):
        if timeframe not in seen:
            managed.append(timeframe)
            seen.add(timeframe)
    return managed


def reconcile_open_paper_positions(
    db,
    executor: OrderExecutionEngine,
    timeframes: list[str],
) -> int:
    markets = (
        db.query(Market)
        .join(Position, Position.market_id == Market.id)
        .filter(
            Position.mode == OrderMode.PAPER.value,
            Position.status == "open",
        )
        .distinct()
        .all()
    )
    closed = 0
    for market in markets:
        for timeframe in timeframes:
            if timeframe not in TIMEFRAME_MS:
                continue
            closed += executor.close_paper_positions_for_timeframe(db, market, timeframe)
    return closed


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
        await run_fast_risk_once(executor, state)
        await asyncio.sleep(max(1, int(settings.fast_risk_loop_interval_seconds)))


def _risk_setting_value(db) -> dict[str, Any]:
    setting = db.get(AppSetting, "risk")
    return dict(setting.value) if setting is not None and isinstance(setting.value, dict) else {}


def _uar_decimal_setting(risk: dict[str, Any], key: str) -> Decimal:
    default = DEFAULT_UAR_RISK_SETTINGS[key]
    parsed = decimal_or_none(risk.get(key, default))
    return parsed if parsed is not None else Decimal(str(default))


def _uar_int_setting(risk: dict[str, Any], key: str) -> int:
    default = int(DEFAULT_UAR_RISK_SETTINGS[key])
    try:
        return max(1, int(risk.get(key, default)))
    except (TypeError, ValueError):
        return default


def _arm_uar_kill_switch(db, ratio_pct: Decimal, block_pct: Decimal) -> None:
    now = datetime.now(UTC).isoformat()
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    value = dict(setting.value) if isinstance(setting.value, dict) else {}
    value.update(
        {
            "enabled": True,
            "reason": "unified_account_ratio_block",
            "triggered_at": now,
            "unified_account_ratio_pct": str(ratio_pct),
            "unified_account_ratio_block_pct": str(block_pct),
        }
    )
    setting.value = value
    db.add(setting)
    db.add(
        DecisionLog(
            event_type="unified_account_ratio_kill_switch_enabled",
            message="Kill switch enabled because Unified Account Ratio is above block threshold",
            data={
                "unified_account_ratio_pct": str(ratio_pct),
                "block_pct": str(block_pct),
            },
        )
    )


def _remote_position_coin(position: dict[str, Any]) -> str | None:
    coin = position.get("coin")
    return coin if isinstance(coin, str) and coin else None


def _select_uar_derisk_position(user_state: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in user_state.get("assetPositions", []):
        if not isinstance(item, dict) or not isinstance(item.get("position"), dict):
            continue
        position = item["position"]
        size = decimal_or_none(position.get("szi"))
        if size is None or size == 0 or _remote_position_coin(position) is None:
            continue
        candidates.append(position)
    if not candidates:
        return None

    def key(position: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        margin_used = decimal_or_none(position.get("marginUsed")) or Decimal("0")
        unrealized_pnl = decimal_or_none(position.get("unrealizedPnl")) or Decimal("0")
        position_value = decimal_or_none(position.get("positionValue")) or Decimal("0")
        return margin_used, -unrealized_pnl, position_value

    return max(candidates, key=key)


def _local_live_positions_for_coin(db, coin: str) -> list[Position]:
    rows = (
        db.query(Position)
        .filter(Position.status == "open", Position.mode == OrderMode.LIVE.value)
        .all()
    )
    matches: list[Position] = []
    for position in rows:
        market = getattr(position, "market", None)
        symbol = getattr(market, "symbol", None)
        metadata = getattr(market, "metadata_json", None)
        hl_coin = metadata.get("hl_coin") if isinstance(metadata, dict) else None
        if symbol == coin or hl_coin == coin:
            matches.append(position)
    return matches


def _cancel_remote_orders_for_coin(
    trading: HyperliquidTradingClient,
    address: str,
    coin: str,
    expires_after_seconds: int,
) -> list[dict[str, Any]]:
    cancelled: list[dict[str, Any]] = []
    for order in trading.frontend_open_orders(address):
        if not isinstance(order, dict) or order.get("coin") != coin:
            continue
        oid = order.get("oid")
        if oid is None:
            cancelled.append({"oid": oid, "status": "skipped_invalid_oid"})
            continue
        try:
            oid_int = int(str(oid))
        except (TypeError, ValueError):
            cancelled.append({"oid": oid, "status": "skipped_invalid_oid"})
            continue
        try:
            response = trading.cancel_order(
                coin=coin,
                oid=oid_int,
                expires_after_seconds=expires_after_seconds,
            )
            cancelled.append({"oid": oid_int, "status": "cancelled", "response": response})
        except Exception as exc:
            cancelled.append({"oid": oid_int, "status": "error", "error": str(exc)})
    return cancelled


def audit_unified_account_ratio_guard(
    db,
    executor: OrderExecutionEngine,
    trading_client: HyperliquidTradingClient | None = None,
) -> dict[str, Any]:
    settings = executor.settings
    address = settings.hyperliquid_account_address
    if not address:
        return {"status": "skipped", "reason": "missing_account_address"}

    risk = _risk_setting_value(db)
    warn_pct = _uar_decimal_setting(risk, "unified_account_ratio_warn_pct")
    block_pct = _uar_decimal_setting(risk, "unified_account_ratio_block_pct")
    derisk_pct = _uar_decimal_setting(risk, "unified_account_ratio_derisk_pct")
    target_pct = _uar_decimal_setting(risk, "unified_account_ratio_target_pct")
    derisk_enabled = bool(
        risk.get(
            "unified_account_ratio_derisk_enabled",
            DEFAULT_UAR_RISK_SETTINGS["unified_account_ratio_derisk_enabled"],
        )
    )
    max_positions = _uar_int_setting(risk, "unified_account_ratio_derisk_max_positions_per_cycle")

    trading = trading_client or HyperliquidTradingClient(settings)
    user_state = trading.user_state(address)
    spot_state = trading.spot_user_state(address)
    balances = spot_state.get("balances", []) if isinstance(spot_state, dict) else []
    free_spot_usdc = free_spot_usdc_from_balances(balances) if isinstance(balances, list) else Decimal("0")
    ratio = calculate_unified_account_ratio(
        user_state,
        free_spot_usdc=free_spot_usdc,
        warn_pct=warn_pct,
        block_pct=block_pct,
        derisk_pct=derisk_pct,
    )
    if ratio.pct is None:
        db.add(
            DecisionLog(
                event_type="unified_account_ratio_guard_unavailable",
                message="Unified Account Ratio unavailable during guard audit",
                data={"source": ratio.source},
            )
        )
        db.commit()
        return {"status": "unavailable", "source": ratio.source}

    result: dict[str, Any] = {
        "status": ratio.status,
        "unified_account_ratio_pct": str(ratio.pct),
        "source": ratio.source,
        "closed": [],
    }

    if ratio.pct >= block_pct:
        _arm_uar_kill_switch(db, ratio.pct, block_pct)

    if not derisk_enabled or ratio.pct < derisk_pct:
        db.commit()
        return result

    live_settings = effective_live_execution_settings(db, settings)
    for _ in range(max_positions):
        position = _select_uar_derisk_position(user_state)
        if position is None:
            db.add(
                DecisionLog(
                    event_type="unified_account_ratio_derisk_no_positions",
                    message="Unified Account Ratio is critical but no remote position is open",
                    data={"unified_account_ratio_pct": str(ratio.pct), "derisk_pct": str(derisk_pct)},
                )
            )
            break
        coin = _remote_position_coin(position)
        size = decimal_or_none(position.get("szi"))
        if coin is None or size is None or size == 0:
            break
        close_result = trading.submit_market_close(
            coin=coin,
            is_buy=size < 0,
            size=abs(size),
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
        cancelled = _cancel_remote_orders_for_coin(
            trading,
            address,
            coin,
            int(live_settings["order_expires_after_seconds"]),
        )
        exit_price = (
            close_result.avg_price
            or decimal_or_none(position.get("markPx"))
            or decimal_or_none(position.get("entryPx"))
        )
        local_closed: list[int] = []
        if exit_price is not None:
            for local_position in _local_live_positions_for_coin(db, coin):
                executor.close_position(
                    db,
                    local_position,
                    exit_price=exit_price,
                    reason="unified_account_ratio_derisk",
                    commit=False,
                )
                if local_position.id is not None:
                    local_closed.append(local_position.id)
        event_data = {
            "coin": coin,
            "side": "long" if size > 0 else "short",
            "size": str(abs(size)),
            "unified_account_ratio_pct": str(ratio.pct),
            "derisk_pct": str(derisk_pct),
            "target_pct": str(target_pct),
            "oid": close_result.oid,
            "status": close_result.status,
            "cancelled_orders": cancelled,
            "trado_positions_closed": local_closed,
        }
        db.add(
            DecisionLog(
                event_type="unified_account_ratio_derisk_close_requested",
                message="Market reduce-only close requested by Unified Account Ratio guard",
                data=event_data,
            )
        )
        result["closed"].append(event_data)
        break

    db.commit()
    return result


def execute_signal_for_mode(
    db,
    executor: OrderExecutionEngine,
    signal,
    operation_mode: OperationMode,
    market: Market,
    timeframe: str,
) -> Position | None:
    if operation_mode == OperationMode.PAPER_TRADING:
        executor.execute_paper(db, signal)
    elif operation_mode == OperationMode.AUTO_TRADE:
        paper_position = executor.execute_paper(db, signal)
        try:
            return executor.execute_live(db, signal)
        except PermissionError as exc:
            if paper_position is None:
                signal.status = SignalStatus.IGNORED.value
                db.add(signal)
                db.commit()
            logger.warning(
                "live_order_rejected",
                market=market.symbol,
                timeframe=timeframe,
                signal_id=signal.id,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception(
                "live_order_failed",
                market=market.symbol,
                timeframe=timeframe,
                signal_id=signal.id,
                error=str(exc),
            )
    return None


_FILL_EVENT_TYPES = frozenset(
    {
        "live_reconciliation_tp1_filled",
        "live_reconciliation_tp2_filled",
        "live_reconciliation_tp3_filled",
        "live_reconciliation_stop_loss_filled",
    }
)


def _is_profitable_close(
    position: "Position",
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> bool:
    if pnl_usd is not None:
        return pnl_usd > 0
    if exit_price is None:
        return False
    side = position.side.lower()
    if side == "long":
        return exit_price > position.entry_price
    if side == "short":
        return exit_price < position.entry_price
    return False


def _display_live_close_reason(
    position: "Position",
    reason: str,
    *,
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> str:
    if reason.lower() != "stop_loss":
        return reason
    if _is_profitable_close(position, exit_price, pnl_usd):
        return "trailing_stop"
    return reason


def _calc_close_card_extras(
    position: "Position",
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
) -> tuple[float | None, int | None]:
    """Restituisce (pnl_pct, duration_seconds) per la share card."""
    pnl_pct: float | None = None
    # Usa la leva contrattuale HL (es. 10x) se disponibile, altrimenti nessun %
    meta = position.metadata_json or {}
    contract_lev = meta.get("contract_leverage")
    lev = float(contract_lev) if contract_lev is not None else None

    if lev and lev > 0:
        if exit_price is not None:
            ep = float(position.entry_price)
            xp = float(exit_price)
            if ep > 0:
                direction = 1 if position.side.upper() == "LONG" else -1
                pnl_pct = direction * (xp - ep) / ep * lev * 100
        elif pnl_usd is not None and position.size:
            # fallback: stima da USD se non abbiamo exit price
            notional = float(position.entry_price) * float(position.size)
            if notional > 0:
                pnl_pct = float(pnl_usd) / notional * lev * 100

    duration: int | None = None
    if position.opened_at:
        ref = position.closed_at or datetime.now(UTC)
        opened = position.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        duration = int((ref - opened).total_seconds())

    return pnl_pct, duration


async def _notify_live_position_closes(
    db: Session,
    notifier: TelegramNotifier,
    since: datetime,
) -> None:
    events = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type.in_(
                [
                    *_FILL_EVENT_TYPES,
                    "position_closed",
                    "position_partially_closed",
                    "live_position_reconciled_closed",
                ]
            ),
            DecisionLog.timestamp >= since,
            DecisionLog.position_id.isnot(None),
        )
        .order_by(DecisionLog.timestamp)
        .all()
    )
    if not events:
        return

    positions_with_fills: set[int] = {
        e.position_id
        for e in events
        if e.event_type in _FILL_EVENT_TYPES and e.position_id is not None
    }

    for event in events:
        if event.position_id is None:
            continue
        position = db.get(Position, event.position_id)
        if position is None:
            continue
        market = position.market
        if market is None:
            continue

        position_meta = position.metadata_json or {}
        raw_source_signal = position_meta.get("source_signal_id")
        try:
            source_signal_id = int(raw_source_signal) if raw_source_signal is not None else None
        except (TypeError, ValueError):
            source_signal_id = None

        try:
            if event.event_type in _FILL_EVENT_TYPES:
                order_type = (
                    event.event_type.replace("live_reconciliation_", "").replace("_filled", "")
                )
                exit_price_str = event.data.get("avg_price")
                exit_price = Decimal(exit_price_str) if exit_price_str else None
                step_pnl_str = event.data.get("closed_pnl")
                step_pnl = Decimal(step_pnl_str) if step_pnl_str else None
                is_partial = position.status == "open"
                remaining_size = position.size if is_partial else None
                total_pnl = (
                    Decimal(str(position.realized_pnl or 0)) if not is_partial else None
                )
                pnl_usd = step_pnl if is_partial else total_pnl
                display_reason = _display_live_close_reason(
                    position,
                    order_type,
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                )
                pnl_pct, duration = _calc_close_card_extras(position, exit_price, pnl_usd)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason=display_reason,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size,
                    step_pnl=step_pnl,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

            elif event.event_type in ("position_closed", "position_partially_closed"):
                if event.data.get("mode") != "live":
                    continue
                exit_price_str = event.data.get("exit_price")
                exit_price = Decimal(exit_price_str) if exit_price_str else None
                step_pnl_str = event.data.get("realized_pnl")
                step_pnl = Decimal(step_pnl_str) if step_pnl_str else None
                remaining_str = event.data.get("remaining_size")
                remaining_size = Decimal(remaining_str) if remaining_str else None
                is_partial = event.event_type == "position_partially_closed"
                cum_str = event.data.get("cumulative_realized_pnl")
                total_pnl = Decimal(cum_str) if cum_str and not is_partial else None
                reason = event.data.get("reason") or "manual"
                pnl_usd = step_pnl if is_partial else total_pnl
                display_reason = _display_live_close_reason(
                    position,
                    reason,
                    exit_price=exit_price,
                    pnl_usd=pnl_usd,
                )
                pnl_pct, duration = _calc_close_card_extras(position, exit_price, pnl_usd)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=event.data.get("side") or position.side,
                    reason=display_reason,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size if is_partial else None,
                    step_pnl=step_pnl if is_partial else None,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

            elif event.event_type == "live_position_reconciled_closed":
                if event.position_id in positions_with_fills:
                    continue
                recon_pnl = Decimal(str(position.realized_pnl or 0))
                pnl_pct, duration = _calc_close_card_extras(position, None, recon_pnl)
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason="reconciliazione hl",
                    entry_price=position.entry_price,
                    exit_price=None,
                    remaining_size=None,
                    step_pnl=None,
                    total_pnl=recon_pnl,
                    is_partial=False,
                    leverage=position.leverage,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration,
                    signal_id=source_signal_id,
                    db=db,
                )

        except Exception:
            logger.warning(
                "telegram_live_close_notify_failed",
                event_type=event.event_type,
                position_id=event.position_id,
            )


async def _notify_btc_macro_reversals(
    db: Session,
    notifier: TelegramNotifier,
    since: datetime,
) -> None:
    """Broadcast BTC macro reversal events logged during the current tick to
    the public signals channel. One Telegram message per detected reversal."""
    events = (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type == "btc_macro_reversal_detected",
            DecisionLog.timestamp >= since,
        )
        .order_by(DecisionLog.timestamp)
        .all()
    )
    if not events:
        return
    for event in events:
        data = event.data or {}
        try:
            await notifier.send_btc_macro_detected(
                from_trend=str(data.get("from_trend") or "—"),
                to_trend=str(data.get("to_trend") or "—"),
                delta_pct=float(data.get("delta_pct") or 0.0),
                vol_ratio=float(data.get("vol_ratio") or 0.0),
                timeframe=str(data.get("timeframe") or "—"),
            )
        except Exception:
            logger.warning(
                "telegram_btc_macro_notify_failed",
                event_id=event.id,
            )


async def _notify_macro_risk_state_change(
    notifier: TelegramNotifier,
    result: dict[str, Any],
) -> None:
    if not result.get("changed"):
        return
    state = result.get("state")
    if not isinstance(state, dict):
        return
    try:
        await notifier.send_macro_risk_state_changed(state)
    except Exception as exc:
        logger.warning("telegram_macro_risk_notify_failed", error=str(exc))


async def run_once(
    signal_engine: SignalEngine,
    candle_sync: CandleSyncService,
    calibration_engine: AutoCalibrationEngine,
    notifier: TelegramNotifier,
    executor: OrderExecutionEngine,
    macro_risk_monitor: MacroRiskMonitor,
    btc_protection: BtcMacroProtectionEngine | None = None,
) -> tuple[int, int]:
    tick_start = datetime.now(UTC)
    settings = get_settings()
    db = SessionLocal()
    # Invalida le cache per-cycle del signal_engine (fundings, L2 book): evitano
    # di rieseguire chiamate HTTP per ogni (market × timeframe) nello stesso tick.
    signal_engine.begin_cycle()
    # Catturato una volta: usa time.time() per POSIX corretto indipendente dal timezone
    now_ms = int(time.time() * 1000)
    enabled_timeframes = effective_enabled_timeframes(db, settings)
    operation_mode = effective_operation_mode(db, settings)
    managed_paper_timeframes = paper_management_timeframes(db, enabled_timeframes)
    active_timeframes = active_timeframes_for_tick(enabled_timeframes, now_ms)
    paper_active_timeframes = active_timeframes_for_tick(managed_paper_timeframes, now_ms)
    logger.info(
        "worker_tick",
        active_timeframes=active_timeframes or None,
        paper_active_timeframes=paper_active_timeframes or None,
    )
    _record_heartbeat("running")
    pnl_updated = 0
    live_reconciled = 0
    try:
        live_reconciled = await asyncio.to_thread(reconcile_live_positions, db)
        pnl_updated = await refresh_open_position_pnls(db)
    except HyperliquidTransientError as exc:
        logger.warning("position_pnl_refresh_transient_failed", error=str(exc))
        db.rollback()
    except Exception as exc:
        logger.exception("position_pnl_refresh_failed", error=str(exc))
        db.rollback()
    closed = await asyncio.to_thread(
        reconcile_open_paper_positions, db, executor, managed_paper_timeframes
    )
    time_stop_result = await asyncio.to_thread(executor.apply_time_stops, db)
    closed += int(time_stop_result.get("paper_closed", 0))
    portfolio_breaker_result = executor.portfolio_guard.update_circuit_breaker(db)
    macro_risk_result = macro_risk_monitor.run(db)
    db.commit()
    calibrated = False
    calibration_attempted = False
    if should_run_calibration(db, closed):
        calibrated = run_calibration(db, calibration_engine)
        calibration_attempted = True
    run_auto_tuner(db)
    btc_protection_updates = 0
    # Nota: btc_protection.run() viene invocato DOPO il loop di generazione segnali
    # (più sotto) — non qui — perché in reverse_mode deve poter agire sui signal flip
    # generati nello stesso ciclo. La protezione legacy live-only era idempotente
    # rispetto all'ordine; con reverse_mode l'ordine conta.
    if not active_timeframes and not paper_active_timeframes:
        try:
            db.add(DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": 0,
                    "timeframes": enabled_timeframes,
                    "paper_management_timeframes": managed_paper_timeframes,
                    "synced": 0,
                    "skipped": 0,
                    "generated": 0,
                    "closed": closed,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_stop_updates": 0,
                    "btc_protection_updates": btc_protection_updates,
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
                    "macro_risk": macro_risk_result,
                    "calibrated": calibrated,
                    "active_timeframes": [],
                    "paper_active_timeframes": [],
                },
            ))
            db.commit()
            await _notify_macro_risk_state_change(notifier, macro_risk_result)
            await _notify_live_position_closes(db, notifier, tick_start)
            await _notify_btc_macro_reversals(db, notifier, tick_start)
        finally:
            db.close()
        return 0, 0
    try:
        markets = await asyncio.to_thread(
            lambda: (
                db.query(Market)
                .filter(Market.active.is_(True))
                .order_by(Market.market_type)
                .limit(500)
                .all()
            )
        )
        if settings.external_quote_enrichment_enabled:
            signal_engine.market_data.prefetch_quotes([m.base for m in markets])
        perp_oi_map = await asyncio.to_thread(fetch_perp_open_interests, settings.info_url)
        # Prefetch parallelo dei fetch HTTP candele per tutte le coppie attive del tick.
        # La latenza di rete è il costo dominante del ciclo: scaricare in parallelo (con
        # concorrenza limitata) invece che una coppia per volta riduce drasticamente la
        # durata del tick. La persistenza resta serializzata sulla sessione condivisa.
        sync_pairs = [
            (market, timeframe)
            for market in markets
            for timeframe in paper_active_timeframes
            if timeframe in TIMEFRAME_MS
        ]
        prefetched_candles = await candle_sync.prefetch(db, sync_pairs)
        # Prefetch L2 books per tutti i market attivi prima del loop segnali: evita
        # le ~N fetch sequenziali (una per coin) dentro generate(), sostituendole con
        # un batch async a concorrenza limitata che pre-popola _cycle_l2_cache.
        if active_timeframes:
            await signal_engine.prefetch_l2_books(markets, settings)
        synced = 0
        skipped = 0
        generated = 0
        live_stop_updates = 0
        last_heartbeat = time.monotonic()
        for market in markets:
            # Keep heartbeat fresh during long ticks (many markets × timeframes)
            # so /health doesn't flip to "stale" while a cycle is still working.
            if time.monotonic() - last_heartbeat > 30:
                _record_heartbeat("running")
                last_heartbeat = time.monotonic()
            hl_coin = (market.metadata_json or {}).get("hl_coin") or market.symbol
            market_oi = perp_oi_map.get(str(hl_coin))
            for timeframe in paper_active_timeframes:
                prefetched = prefetched_candles.get((market.id, timeframe))
                try:
                    if isinstance(prefetched, Exception):
                        raise prefetched
                    await asyncio.to_thread(
                        candle_sync.persist_candles,
                        db,
                        market,
                        timeframe,
                        prefetched or [],
                        market_oi,
                    )
                except HyperliquidTransientError as exc:
                    skipped += 1
                    logger.warning(
                        "candle_sync_transient_failed",
                        market=market.symbol,
                        market_type=market.market_type,
                        timeframe=timeframe,
                        error=str(exc),
                    )
                    db.rollback()
                    continue
                except Exception as exc:
                    skipped += 1
                    logger.exception(
                        "candle_sync_failed",
                        market=market.symbol,
                        market_type=market.market_type,
                        timeframe=timeframe,
                        error=str(exc),
                    )
                    db.rollback()
                    continue
                synced += 1
                if (market.metadata_json or {}).get("monitoring_only"):
                    # Market osservato solo a scopo macro (es. XYZ100): candele
                    # sincronizzate sopra, ma nessun segnale né posizione.
                    continue
                closed += await asyncio.to_thread(
                    executor.close_paper_positions_for_timeframe, db, market, timeframe
                )
                if timeframe not in active_timeframes:
                    continue
                if operation_mode == OperationMode.AUTO_TRADE:
                    live_stop_updates += await asyncio.to_thread(
                        executor.position_manager.manage_live_positions_for_timeframe,
                        db,
                        market,
                        timeframe,
                    )
                signal = await asyncio.to_thread(signal_engine.generate, db, market, timeframe)
                if signal is None:
                    continue
                generated += 1
                logger.info(
                    "signal_generated",
                    market=market.symbol,
                    timeframe=timeframe,
                    direction=signal.direction,
                    score=signal.score,
                    entry=str(signal.entry_price),
                )
                alert_min_score = effective_alert_min_score(db, settings)
                if signal.score >= alert_min_score:
                    await notifier.send_signal(db, signal)
                live_position = execute_signal_for_mode(db, executor, signal, operation_mode, market, timeframe)
                if live_position is not None:
                    try:
                        await notifier.send_live_position_opened(live_position)
                    except Exception:
                        logger.exception("telegram_live_open_notify_failed")
        if not calibration_attempted and should_run_calibration(db, closed):
            calibrated = run_calibration(db, calibration_engine)
            calibration_attempted = True
        if btc_protection is not None and operation_mode in {
            OperationMode.AUTO_TRADE,
            OperationMode.PAPER_TRADING,
        }:
            try:
                btc_protection_updates = await asyncio.to_thread(
                    btc_protection.run, db, executor
                )
            except Exception as exc:
                logger.exception("btc_macro_protection_failed", error=str(exc))
                db.rollback()
        mark_timeframes_run(paper_active_timeframes, now_ms)
        db.add(
            DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": len(markets),
                    "timeframes": enabled_timeframes,
                    "paper_management_timeframes": managed_paper_timeframes,
                    "active_timeframes": active_timeframes,
                    "paper_active_timeframes": paper_active_timeframes,
                    "synced": synced,
                    "skipped": skipped,
                    "generated": generated,
                    "closed": closed,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_stop_updates": live_stop_updates,
                    "btc_protection_updates": btc_protection_updates,
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
                    "macro_risk": macro_risk_result,
                    "calibrated": calibrated,
                    # Aggregato dei motivi per cui i segnali non sono stati emessi —
                    # in produzione gli eventi debug per skip sono filtrati, qui
                    # restano visibili per debug "perché non genero?".
                    "skip_reasons": signal_engine.cycle_skip_counts(),
                },
            )
        )
        db.commit()
        await _notify_macro_risk_state_change(notifier, macro_risk_result)
        await _notify_live_position_closes(db, notifier, tick_start)
        await _notify_btc_macro_reversals(db, notifier, tick_start)
        return generated, skipped
    finally:
        db.close()


async def main() -> None:
    configure_logging()
    settings = get_settings()
    if settings.operation_mode == OperationMode.AUTO_TRADE and settings.enable_live_trading:
        missing = []
        if not settings.hyperliquid_private_key:
            missing.append("HYPERLIQUID_PRIVATE_KEY")
        if not settings.hyperliquid_account_address:
            missing.append("HYPERLIQUID_ACCOUNT_ADDRESS")
        if missing:
            raise RuntimeError(
                "AUTO_TRADE with enable_live_trading=True requires: " + ", ".join(missing)
            )
    create_schema()
    apply_migrations()
    db = SessionLocal()
    try:
        seed_defaults(db)
        if db.query(Market).count() == 0:
            await MarketSyncService().sync(db)
    finally:
        db.close()
    signal_engine = SignalEngine()
    candle_sync = CandleSyncService()
    calibration_engine = AutoCalibrationEngine()
    notifier = TelegramNotifier(settings)
    executor = OrderExecutionEngine(settings)
    macro_risk_monitor = MacroRiskMonitor(settings)
    btc_protection = BtcMacroProtectionEngine(settings)
    _record_heartbeat("starting")

    scheduler = PeriodicScheduler(time.time())
    stats = _WorkerStats()
    fast_risk_task = asyncio.create_task(run_fast_risk_loop(executor))

    startup_db = SessionLocal()
    try:
        await notifier.send_worker_online(startup_db)
    except Exception:
        logger.exception("telegram_worker_online_failed")
    finally:
        startup_db.close()

    while True:
        if fast_risk_task.done():
            try:
                fast_risk_task.result()
            except Exception as exc:
                logger.exception("fast_risk_loop_stopped", error=str(exc))
            fast_risk_task = asyncio.create_task(run_fast_risk_loop(executor))
        if _TICK_LOCK.locked():
            logger.warning("worker_tick_skipped_overlap")
        else:
            async with _TICK_LOCK:
                _record_heartbeat("tick")
                try:
                    generated, candle_errors = await run_once(
                        signal_engine,
                        candle_sync,
                        calibration_engine,
                        notifier,
                        executor,
                        macro_risk_monitor,
                        btc_protection,
                    )
                    stats.cycles += 1
                    stats.signals_generated += generated
                    stats.candle_errors += candle_errors
                except Exception as exc:
                    logger.exception("worker_cycle_failed", error=str(exc))
                    _record_worker_failure(exc)
                    stats.cycle_failures += 1
                    now = time.time()
                    if scheduler.due_error_notify(now):
                        try:
                            await notifier.send_worker_error("worker_cycle", exc)
                        except Exception:
                            logger.exception("telegram_error_notify_failed")

        now = time.time()
        if scheduler.due_recap(now):
            period_min = round((now - stats.period_start) / 60)
            live_balance = None
            try:
                live_balance = await asyncio.to_thread(
                    HyperliquidTradingClient(settings).account_value
                )
            except Exception:
                logger.warning("recap_live_balance_fetch_failed")
            recap_db = SessionLocal()
            try:
                await notifier.send_hourly_recap(recap_db, {
                    "cycles": stats.cycles,
                    "signals_generated": stats.signals_generated,
                    "candle_errors": stats.candle_errors,
                    "cycle_failures": stats.cycle_failures,
                    "period_minutes": period_min,
                    "period_start": stats.period_start,
                    "live_balance": live_balance,
                })
            except Exception:
                logger.exception("telegram_recap_failed")
            finally:
                recap_db.close()
            stats = _WorkerStats()

        if scheduler.due_daily_recap(now):
            recap_db = SessionLocal()
            try:
                window_end = datetime.fromtimestamp(now, tz=UTC)
                window_start = window_end - timedelta(hours=24)
                await notifier.send_daily_recap(
                    recap_db,
                    window_start=window_start,
                    window_end=window_end,
                )
            except Exception:
                logger.exception("telegram_daily_recap_failed")
            finally:
                recap_db.close()

        if scheduler.due_diagnostic(now):
            diag_db = SessionLocal()
            try:
                await DiagnosticReporter().send(diag_db, notifier)
                _record_system_cycle_status(
                    diag_db,
                    "diagnostic_report_cycle",
                    "Diagnostic Report Cycle",
                    "ok",
                    message="Diagnostic report sent",
                    expected_interval_seconds=DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
                )
                diag_db.commit()
            except Exception as exc:
                logger.exception("telegram_diagnostic_report_failed")
                diag_db.rollback()
                try:
                    data = {"error": str(exc), "type": type(exc).__name__}
                    _record_system_cycle_status(
                        diag_db,
                        "diagnostic_report_cycle",
                        "Diagnostic Report Cycle",
                        "failed",
                        message="Diagnostic report failed",
                        data=data,
                        expected_interval_seconds=DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
                    )
                    diag_db.add(
                        DecisionLog(
                            event_type="diagnostic_report_cycle_failed",
                            message="Diagnostic report cycle failed",
                            data=data,
                        )
                    )
                    diag_db.commit()
                except Exception:
                    logger.exception("diagnostic_report_cycle_failure_log_failed")
            finally:
                diag_db.close()

        if scheduler.due_data_retention(now):
            retention_db = SessionLocal()
            try:
                result = await asyncio.to_thread(prune_old_data, retention_db)
                _record_system_cycle_status(
                    retention_db,
                    "data_retention_cycle",
                    "Data Retention Cycle",
                    "ok",
                    message="Data retention cycle completed",
                    data=result,
                    expected_interval_seconds=DATA_RETENTION_INTERVAL_SECONDS,
                )
                retention_db.commit()
                logger.info("data_retention_cycle_completed", **result)
            except Exception as exc:
                logger.exception("data_retention_cycle_failed")
                retention_db.rollback()
                try:
                    data = {"error": str(exc), "type": type(exc).__name__}
                    _record_system_cycle_status(
                        retention_db,
                        "data_retention_cycle",
                        "Data Retention Cycle",
                        "failed",
                        message="Data retention cycle failed",
                        data=data,
                        expected_interval_seconds=DATA_RETENTION_INTERVAL_SECONDS,
                    )
                    retention_db.add(
                        DecisionLog(
                            event_type="data_retention_cycle_failed",
                            message="Data retention cycle failed",
                            data=data,
                        )
                    )
                    retention_db.commit()
                except Exception:
                    logger.exception("data_retention_cycle_failure_log_failed")
            finally:
                retention_db.close()

        if scheduler.due_live_liquidation_guard(now):
            liq_db = SessionLocal()
            try:
                result = await asyncio.to_thread(
                    executor.position_manager.audit_live_positions_against_liquidation,
                    liq_db,
                )
                _record_system_cycle_status(
                    liq_db,
                    "live_liquidation_guard_cycle",
                    "Live Liquidation Guard Cycle",
                    "ok",
                    message="Live liquidation guard cycle completed",
                    data=result,
                    expected_interval_seconds=LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
                )
                liq_db.commit()
                logger.info("live_liquidation_guard_cycle_completed", **result)
            except Exception as exc:
                logger.exception("live_liquidation_guard_cycle_failed", error=str(exc))
                liq_db.rollback()
                try:
                    data = {"error": str(exc), "type": type(exc).__name__}
                    _record_system_cycle_status(
                        liq_db,
                        "live_liquidation_guard_cycle",
                        "Live Liquidation Guard Cycle",
                        "failed",
                        message="Live liquidation guard cycle failed",
                        data=data,
                        expected_interval_seconds=LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
                    )
                    liq_db.add(
                        DecisionLog(
                            event_type="live_liquidation_guard_cycle_failed",
                            message="Live liquidation guard cycle failed",
                            data=data,
                        )
                    )
                    liq_db.commit()
                except Exception:
                    logger.exception("live_liquidation_guard_cycle_failure_log_failed")
            finally:
                liq_db.close()

        if scheduler.due_unified_account_ratio_guard(now):
            uar_db = SessionLocal()
            try:
                result = await asyncio.to_thread(
                    audit_unified_account_ratio_guard,
                    uar_db,
                    executor,
                )
                cycle_status = "skipped" if result.get("status") == "skipped" else "ok"
                _record_system_cycle_status(
                    uar_db,
                    "unified_account_ratio_guard_cycle",
                    "Unified Account Ratio Guard Cycle",
                    cycle_status,
                    message="Unified Account Ratio guard cycle completed",
                    data=result,
                    expected_interval_seconds=UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
                )
                uar_db.commit()
                logger.info("unified_account_ratio_guard_cycle_completed", **result)
            except Exception as exc:
                logger.exception("unified_account_ratio_guard_cycle_failed", error=str(exc))
                uar_db.rollback()
                try:
                    data = {"error": str(exc), "type": type(exc).__name__}
                    _record_system_cycle_status(
                        uar_db,
                        "unified_account_ratio_guard_cycle",
                        "Unified Account Ratio Guard Cycle",
                        "failed",
                        message="Unified Account Ratio guard cycle failed",
                        data=data,
                        expected_interval_seconds=UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
                    )
                    uar_db.add(
                        DecisionLog(
                            event_type="unified_account_ratio_guard_cycle_failed",
                            message="Unified Account Ratio guard cycle failed",
                            data=data,
                        )
                    )
                    uar_db.commit()
                except Exception:
                    logger.exception("unified_account_ratio_guard_cycle_failure_log_failed")
            finally:
                uar_db.close()

        _record_heartbeat("idle")
        await asyncio.sleep(60)


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


if __name__ == "__main__":
    asyncio.run(main())
