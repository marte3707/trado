import asyncio
import dataclasses
import time
from datetime import datetime, timezone

import structlog

from app.core.config import get_settings
from app.core.enums import OperationMode, OrderMode, SignalStatus
from app.core.logging import configure_logging
from app.core.runtime_settings import effective_alert_min_score, effective_enabled_timeframes, effective_operation_mode
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.models import AppSetting, DecisionLog, Market, Position
from app.db.session import SessionLocal
from app.engine.auto_tuner import AutoTuner
from app.engine.calibration import AutoCalibrationEngine
from app.engine.execution import OrderExecutionEngine
from app.engine.signal_engine import SignalEngine
from app.notifications.telegram import TelegramNotifier
from app.services.candle_sync import CandleSyncService, TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidTransientError
from app.services.live_reconciliation import reconcile_live_positions
from app.services.market_sync import MarketSyncService
from app.services.position_pnl import refresh_open_position_pnls

logger = structlog.get_logger()
_LAST_TIMEFRAME_RUN_MS: dict[str, int] = {}
_TICK_LOCK = asyncio.Lock()

_RECAP_INTERVAL_SECONDS = 3600  # ogni ora
_ERROR_NOTIFY_COOLDOWN_SECONDS = 1800  # max 1 notifica errore ogni 30 min
_next_recap_at: float = 0.0
_last_error_notify_at: float = 0.0


@dataclasses.dataclass
class _WorkerStats:
    cycles: int = 0
    signals_generated: int = 0
    candle_errors: int = 0
    cycle_failures: int = 0
    period_start: float = dataclasses.field(default_factory=time.time)


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
    try:
        return int(value["last_trade_count"])
    except (KeyError, TypeError, ValueError):
        return None


def should_run_calibration(db, closed_this_cycle: int) -> bool:
    if closed_this_cycle > 0:
        return True
    total_closed = closed_paper_trade_count(db)
    if total_closed < calibration_min_closed_trades(db):
        return False
    last_count = last_calibration_trade_count(db)
    return last_count is None or last_count != total_closed


def run_calibration(db, calibration_engine: AutoCalibrationEngine) -> bool:
    try:
        return calibration_engine.run(db).eligible
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


async def run_once(
    signal_engine: SignalEngine,
    candle_sync: CandleSyncService,
    calibration_engine: AutoCalibrationEngine,
    notifier: TelegramNotifier,
    executor: OrderExecutionEngine,
) -> tuple[int, int]:
    settings = get_settings()
    db = SessionLocal()
    # Catturato una volta: usa time.time() per POSIX corretto indipendente dal timezone
    now_ms = int(time.time() * 1000)
    enabled_timeframes = effective_enabled_timeframes(db, settings)
    operation_mode = effective_operation_mode(db, settings)
    active_timeframes = active_timeframes_for_tick(enabled_timeframes, now_ms)
    logger.info("worker_tick", active_timeframes=active_timeframes or None)
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
        reconcile_open_paper_positions, db, executor, enabled_timeframes
    )
    calibrated = False
    calibration_attempted = False
    if should_run_calibration(db, closed):
        calibrated = run_calibration(db, calibration_engine)
        calibration_attempted = True
    run_auto_tuner(db)
    if not active_timeframes:
        try:
            db.add(DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": 0,
                    "timeframes": enabled_timeframes,
                    "synced": 0,
                    "skipped": 0,
                    "generated": 0,
                    "closed": closed,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_stop_updates": 0,
                    "calibrated": calibrated,
                    "active_timeframes": [],
                },
            ))
            db.commit()
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
            for timeframe in active_timeframes:
                try:
                    await candle_sync.sync_market(db, market, timeframe)
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
                    await asyncio.sleep(0.25)
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
                signal = await asyncio.to_thread(signal_engine.generate, db, market, timeframe)
                if signal is None:
                    await asyncio.sleep(0.05)
                    continue
                if operation_mode in (OperationMode.PAPER_TRADING, OperationMode.AUTO_TRADE):
                    position_mode = (
                        OrderMode.PAPER.value
                        if operation_mode == OperationMode.PAPER_TRADING
                        else OrderMode.LIVE.value
                    )
                    if (
                        db.query(Position)
                        .filter(
                            Position.market_id == market.id,
                            Position.mode == position_mode,
                            Position.status == "open",
                        )
                        .first()
                        is not None
                    ):
                        signal.status = SignalStatus.IGNORED.value
                        db.commit()
                        await asyncio.sleep(0.05)
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
                if operation_mode == OperationMode.PAPER_TRADING:
                    executor.execute_paper(db, signal)
                elif operation_mode == OperationMode.AUTO_TRADE:
                    try:
                        executor.execute_live(db, signal)
                    except PermissionError as exc:
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
                await asyncio.sleep(0.05)
        if not calibration_attempted and should_run_calibration(db, closed):
            calibrated = run_calibration(db, calibration_engine)
            calibration_attempted = True
        mark_timeframes_run(active_timeframes, now_ms)
        db.add(
            DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": len(markets),
                    "timeframes": enabled_timeframes,
                    "active_timeframes": active_timeframes,
                    "synced": synced,
                    "skipped": skipped,
                    "generated": generated,
                    "closed": closed,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_stop_updates": live_stop_updates,
                    "calibrated": calibrated,
                },
            )
        )
        db.commit()
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
    _record_heartbeat("starting")

    global _next_recap_at, _last_error_notify_at
    _next_recap_at = time.time() + _RECAP_INTERVAL_SECONDS
    _last_error_notify_at = 0.0
    stats = _WorkerStats()

    startup_db = SessionLocal()
    try:
        await notifier.send_worker_online(startup_db)
    except Exception:
        logger.exception("telegram_worker_online_failed")
    finally:
        startup_db.close()

    while True:
        if _TICK_LOCK.locked():
            logger.warning("worker_tick_skipped_overlap")
        else:
            async with _TICK_LOCK:
                _record_heartbeat("tick")
                try:
                    generated, candle_errors = await run_once(
                        signal_engine, candle_sync, calibration_engine, notifier, executor
                    )
                    stats.cycles += 1
                    stats.signals_generated += generated
                    stats.candle_errors += candle_errors
                except Exception as exc:
                    logger.exception("worker_cycle_failed", error=str(exc))
                    _record_worker_failure(exc)
                    stats.cycle_failures += 1
                    now = time.time()
                    if now - _last_error_notify_at >= _ERROR_NOTIFY_COOLDOWN_SECONDS:
                        _last_error_notify_at = now
                        try:
                            await notifier.send_worker_error("worker_cycle", exc)
                        except Exception:
                            logger.exception("telegram_error_notify_failed")

        now = time.time()
        if now >= _next_recap_at:
            _next_recap_at = now + _RECAP_INTERVAL_SECONDS
            period_min = round((now - stats.period_start) / 60)
            recap_db = SessionLocal()
            try:
                await notifier.send_hourly_recap(recap_db, {
                    "cycles": stats.cycles,
                    "signals_generated": stats.signals_generated,
                    "candle_errors": stats.candle_errors,
                    "cycle_failures": stats.cycle_failures,
                    "period_minutes": period_min,
                })
            except Exception:
                logger.exception("telegram_recap_failed")
            finally:
                recap_db.close()
            stats = _WorkerStats()

        await asyncio.sleep(60)


def _record_heartbeat(phase: str) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).isoformat()
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
