import asyncio
import time

import structlog

from app.core.config import get_settings
from app.core.enums import OperationMode, OrderMode
from app.core.logging import configure_logging
from app.core.runtime_settings import effective_enabled_timeframes, effective_operation_mode
from app.db.init_db import create_schema, seed_defaults
from app.db.models import DecisionLog, Market, Position
from app.db.session import SessionLocal
from app.engine.calibration import AutoCalibrationEngine
from app.engine.execution import OrderExecutionEngine
from app.engine.signal_engine import SignalEngine
from app.notifications.telegram import TelegramNotifier
from app.services.candle_sync import CandleSyncService, TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidTransientError
from app.services.market_sync import MarketSyncService

logger = structlog.get_logger()
_LAST_TIMEFRAME_RUN_MS: dict[str, int] = {}


def should_run_timeframe(timeframe: str, now_ms: int) -> bool:
    interval_ms = TIMEFRAME_MS[timeframe]
    return now_ms % interval_ms < 60_000


def active_timeframes_for_tick(timeframes: list[str], now_ms: int) -> list[str]:
    active: list[str] = []
    for timeframe in timeframes:
        if timeframe not in TIMEFRAME_MS:
            continue
        interval_ms = TIMEFRAME_MS[timeframe]
        last_run_ms = _LAST_TIMEFRAME_RUN_MS.get(timeframe)
        if should_run_timeframe(timeframe, now_ms) or (
            last_run_ms is not None and now_ms - last_run_ms >= interval_ms
        ):
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


async def run_once() -> None:
    settings = get_settings()
    candle_sync = CandleSyncService()
    signal_engine = SignalEngine()
    calibration_engine = AutoCalibrationEngine()
    notifier = TelegramNotifier(settings)
    executor = OrderExecutionEngine(settings)
    db = SessionLocal()
    # Catturato una volta: usa time.time() per POSIX corretto indipendente dal timezone
    now_ms = int(time.time() * 1000)
    enabled_timeframes = effective_enabled_timeframes(db, settings)
    operation_mode = effective_operation_mode(db, settings)
    active_timeframes = active_timeframes_for_tick(enabled_timeframes, now_ms)
    logger.info("worker_tick", active_timeframes=active_timeframes or None)
    closed = reconcile_open_paper_positions(db, executor, enabled_timeframes)
    calibrated = False
    if closed:
        calibrated = calibration_engine.run(db).eligible
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
                    "calibrated": calibrated,
                    "active_timeframes": [],
                },
            ))
            db.commit()
        finally:
            db.close()
        return
    try:
        markets = (
            db.query(Market)
            .filter(Market.active.is_(True))
            .order_by(Market.market_type)  # perp prima di spot
            .limit(500)
            .all()
        )
        synced = 0
        skipped = 0
        generated = 0
        for market in markets:
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
                closed += executor.close_paper_positions_for_timeframe(db, market, timeframe)
                signal = signal_engine.generate(db, market, timeframe)
                if signal is None:
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
                await notifier.send_signal(db, signal)
                if operation_mode == OperationMode.PAPER_TRADING:
                    executor.execute_paper(db, signal)
                await asyncio.sleep(0.05)
        if closed and not calibrated:
            calibrated = calibration_engine.run(db).eligible
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
                    "calibrated": calibrated,
                },
            )
        )
        db.commit()
    finally:
        db.close()


async def main() -> None:
    configure_logging()
    create_schema()
    db = SessionLocal()
    try:
        seed_defaults(db)
        if db.query(Market).count() == 0:
            await MarketSyncService().sync(db)
    finally:
        db.close()
    while True:
        try:
            await run_once()
        except Exception as exc:
            logger.exception("worker_cycle_failed", error=str(exc))
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
