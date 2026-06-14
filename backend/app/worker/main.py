import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import get_settings
from app.core.enums import OperationMode, OrderMode, SignalStatus
from app.core.logging import configure_logging
from app.core.runtime_settings import (
    effective_alert_min_score,
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
    effective_operation_mode,
)
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.models import DecisionLog, Market, Position
from app.db.session import SessionLocal
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
from app.worker.calibration_runner import (
    run_auto_tuner,
    run_calibration,
    should_run_calibration,
)
from app.worker.cycle_status import (
    _record_heartbeat,
    _record_worker_failure,
    run_periodic_cycle,
)
from app.worker.fast_risk import _FastRiskState, _last_cycle_ok_at, run_fast_risk_loop
from app.worker.live_close_notifications import (
    _notify_btc_macro_reversals,
    _notify_live_position_closes,
    _notify_macro_risk_state_change,
)
from app.worker.scheduler import (
    DATA_RETENTION_INTERVAL_SECONDS,
    DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
    LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
    UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
    PeriodicScheduler,
    next_daily_recap_timestamp,
)
from app.worker.timeframe_schedule import (
    active_timeframes_for_tick,
    mark_timeframes_run,
    paper_management_timeframes,
)
from app.worker.uar_guard import audit_unified_account_ratio_guard

# Re-export per retro-compatibilità: alcuni test referenziano
# `app.worker.main.next_daily_recap_timestamp`.
__all__ = ["next_daily_recap_timestamp"]

logger = structlog.get_logger()
_TICK_LOCK = asyncio.Lock()


async def _send_diagnostic_report(db, notifier: TelegramNotifier) -> None:
    await DiagnosticReporter().send(db, notifier)


@dataclasses.dataclass
class _WorkerStats:
    cycles: int = 0
    signals_generated: int = 0
    candle_errors: int = 0
    cycle_failures: int = 0
    period_start: float = dataclasses.field(default_factory=time.time)


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
        if paper_position is None:
            signal.status = SignalStatus.IGNORED.value
            db.add(signal)
            db.add(
                DecisionLog(
                    event_type="live_order_skipped_after_paper_skip",
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    message="Live order skipped because paper entry was not created",
                    data={
                        "market": market.symbol,
                        "timeframe": timeframe,
                        "operation_mode": operation_mode.value,
                    },
                )
            )
            db.commit()
            logger.warning(
                "live_order_skipped_after_paper_skip",
                market=market.symbol,
                timeframe=timeframe,
                signal_id=signal.id,
            )
            return None
        try:
            return executor.execute_live(db, signal)
        except PermissionError as exc:
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


def should_publish_public_signal(signal, alert_min_score: int) -> bool:
    return signal.score >= alert_min_score and signal.status != SignalStatus.IGNORED.value


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
    monitoring_only_timeframes = effective_monitoring_only_timeframes(db)
    sync_timeframes = list(dict.fromkeys([*enabled_timeframes, *monitoring_only_timeframes]))
    operation_mode = effective_operation_mode(db, settings)
    managed_sync_timeframes = paper_management_timeframes(db, sync_timeframes)
    active_timeframes = active_timeframes_for_tick(enabled_timeframes, now_ms)
    sync_active_timeframes = active_timeframes_for_tick(managed_sync_timeframes, now_ms)
    logger.info(
        "worker_tick",
        active_timeframes=active_timeframes or None,
        monitoring_only_timeframes=monitoring_only_timeframes or None,
        sync_active_timeframes=sync_active_timeframes or None,
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
        reconcile_open_paper_positions, db, executor, managed_sync_timeframes
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
    if not active_timeframes and not sync_active_timeframes:
        try:
            db.add(DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": 0,
                    "timeframes": enabled_timeframes,
                    "monitoring_only_timeframes": monitoring_only_timeframes,
                    "sync_timeframes": sync_timeframes,
                    "paper_management_timeframes": managed_sync_timeframes,
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
                    "sync_active_timeframes": [],
                },
            ))
            db.commit()
            await _notify_macro_risk_state_change(db, notifier, macro_risk_result)
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
            for timeframe in sync_active_timeframes
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
            for timeframe in sync_active_timeframes:
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
                if operation_mode == OperationMode.AUTO_TRADE:
                    live_stop_updates += await asyncio.to_thread(
                        executor.position_manager.manage_live_positions_for_timeframe,
                        db,
                        market,
                        timeframe,
                    )
                if timeframe not in active_timeframes:
                    continue
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
                live_position = execute_signal_for_mode(db, executor, signal, operation_mode, market, timeframe)
                if should_publish_public_signal(signal, alert_min_score):
                    await notifier.send_signal(db, signal)
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
        mark_timeframes_run(sync_active_timeframes, now_ms)
        db.add(
            DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": len(markets),
                    "timeframes": enabled_timeframes,
                    "monitoring_only_timeframes": monitoring_only_timeframes,
                    "sync_timeframes": sync_timeframes,
                    "paper_management_timeframes": managed_sync_timeframes,
                    "active_timeframes": active_timeframes,
                    "sync_active_timeframes": sync_active_timeframes,
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
        await _notify_macro_risk_state_change(db, notifier, macro_risk_result)
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

    sched_db = SessionLocal()
    try:
        last_diagnostic_at = _last_cycle_ok_at(sched_db, "diagnostic_report_cycle")
        last_data_retention_at = _last_cycle_ok_at(sched_db, "data_retention_cycle")
    finally:
        sched_db.close()
    scheduler = PeriodicScheduler(
        time.time(),
        last_diagnostic_at=last_diagnostic_at,
        last_data_retention_at=last_data_retention_at,
    )
    stats = _WorkerStats()
    fast_risk_state = _FastRiskState()
    fast_risk_task = asyncio.create_task(run_fast_risk_loop(executor, fast_risk_state))

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
            fast_risk_task = asyncio.create_task(run_fast_risk_loop(executor, fast_risk_state))
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
            await run_periodic_cycle(
                cycle_id="diagnostic_report_cycle",
                label="Diagnostic Report Cycle",
                expected_interval_seconds=DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
                work=lambda db: _send_diagnostic_report(db, notifier),
                ok_message="Diagnostic report sent",
                failed_message="Diagnostic report failed",
            )

        if scheduler.due_data_retention(now):
            await run_periodic_cycle(
                cycle_id="data_retention_cycle",
                label="Data Retention Cycle",
                expected_interval_seconds=DATA_RETENTION_INTERVAL_SECONDS,
                work=lambda db: asyncio.to_thread(prune_old_data, db),
                ok_message="Data retention cycle completed",
                failed_message="Data retention cycle failed",
            )

        if scheduler.due_live_liquidation_guard(now):
            await run_periodic_cycle(
                cycle_id="live_liquidation_guard_cycle",
                label="Live Liquidation Guard Cycle",
                expected_interval_seconds=LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
                work=lambda db: asyncio.to_thread(
                    executor.position_manager.audit_live_positions_against_liquidation,
                    db,
                ),
                ok_message="Live liquidation guard cycle completed",
                failed_message="Live liquidation guard cycle failed",
            )

        if scheduler.due_unified_account_ratio_guard(now):
            await run_periodic_cycle(
                cycle_id="unified_account_ratio_guard_cycle",
                label="Unified Account Ratio Guard Cycle",
                expected_interval_seconds=UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
                work=lambda db: asyncio.to_thread(
                    audit_unified_account_ratio_guard, db, executor
                ),
                ok_message="Unified Account Ratio guard cycle completed",
                failed_message="Unified Account Ratio guard cycle failed",
                ok_status=lambda result: (
                    "skipped" if (result or {}).get("status") == "skipped" else "ok"
                ),
            )

        _record_heartbeat("idle")
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
