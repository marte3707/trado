import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import get_settings
from app.core.enums import MarketType, OperationMode, OrderMode, SignalStatus
from app.core.logging import configure_logging
from app.core.runtime_settings import (
    effective_alert_min_score,
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
    effective_operation_mode,
)
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.models import AppSetting, DecisionLog, Market, Position, Signal
from app.db.session import SessionLocal
from app.engine.btc_macro_protection import BtcMacroProtectionEngine
from app.engine.calibration import AutoCalibrationEngine
from app.engine.execution import OrderExecutionEngine
from app.engine.macro_risk_monitor import MacroRiskMonitor
from app.engine.signal_engine import SignalEngine
from app.notifications.diagnostic_report import DiagnosticReporter
from app.notifications.telegram import TelegramNotifier
from app.services.candle_sync import (
    DEFAULT_CANDLE_PREFETCH_BUDGET,
    DEFAULT_CANDLE_RETRY_ATTEMPTS,
    TIMEFRAME_MS,
    CandleSyncService,
)
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
    _notify_stop_update_failures,
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


HYPERLIQUID_RATE_LIMIT_SETTING_KEY = "hyperliquid_rate_limit"
WORKER_MARKET_SYNC_CURSOR_KEY = "worker_market_sync_cursor"
RECENT_SIGNAL_PRIORITY_LOOKBACK_SECONDS = 6 * 60 * 60
DEFAULT_FAST_RISK_RECENT_RECONCILE_TTL_SECONDS = 90
DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS = {
    "candle_prefetch_budget_per_tick": DEFAULT_CANDLE_PREFETCH_BUDGET,
    "candle_retry_attempts": DEFAULT_CANDLE_RETRY_ATTEMPTS,
    "fast_risk_recent_reconcile_ttl_seconds": DEFAULT_FAST_RISK_RECENT_RECONCILE_TTL_SECONDS,
}


def _int_setting(value: object, default: int, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(minimum, value)
    if not isinstance(value, str):
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, parsed)


def effective_hyperliquid_rate_limit_settings(db) -> dict[str, int]:
    setting = db.get(AppSetting, HYPERLIQUID_RATE_LIMIT_SETTING_KEY)
    raw = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    return {
        "candle_prefetch_budget_per_tick": _int_setting(
            raw.get("candle_prefetch_budget_per_tick"),
            DEFAULT_CANDLE_PREFETCH_BUDGET,
        ),
        "candle_retry_attempts": _int_setting(
            raw.get("candle_retry_attempts"),
            DEFAULT_CANDLE_RETRY_ATTEMPTS,
        ),
        "fast_risk_recent_reconcile_ttl_seconds": _int_setting(
            raw.get("fast_risk_recent_reconcile_ttl_seconds"),
            DEFAULT_FAST_RISK_RECENT_RECONCILE_TTL_SECONDS,
        ),
    }


def _open_position_market_ids(db) -> set[int]:
    rows = (
        db.query(Position.market_id)
        .filter(
            Position.status == "open",
            Position.mode.in_([OrderMode.LIVE.value, OrderMode.PAPER.value]),
        )
        .distinct()
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _recent_signal_market_ids(db, now: datetime) -> set[int]:
    cutoff = now - timedelta(seconds=RECENT_SIGNAL_PRIORITY_LOOKBACK_SECONDS)
    rows = (
        db.query(Signal.market_id)
        .filter(Signal.created_at >= cutoff)
        .distinct()
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _cursor_offset(db) -> int:
    setting = db.get(AppSetting, WORKER_MARKET_SYNC_CURSOR_KEY)
    raw = setting.value if setting is not None else {}
    raw_offset = raw.get("offset", 0) if isinstance(raw, dict) else raw
    return _int_setting(raw_offset, 0, minimum=0)


def _store_cursor_offset(db, offset: int, *, universe_size: int) -> None:
    value = {
        "offset": max(0, int(offset)),
        "universe_size": max(0, int(universe_size)),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    setting = db.get(AppSetting, WORKER_MARKET_SYNC_CURSOR_KEY)
    if setting is None:
        db.add(AppSetting(key=WORKER_MARKET_SYNC_CURSOR_KEY, value=value))
    else:
        setting.value = value


def select_worker_sync_pairs(
    db,
    markets: list[Market],
    timeframes: list[str],
    *,
    budget: int,
    now: datetime,
) -> tuple[list[tuple[Market, str]], dict[str, int]]:
    valid_timeframes = [timeframe for timeframe in timeframes if timeframe in TIMEFRAME_MS]
    perp_markets = [
        market
        for market in markets
        if market.active and market.market_type == MarketType.PERP.value
    ]
    total_pairs = len(perp_markets) * len(valid_timeframes)
    if not perp_markets or not valid_timeframes or budget <= 0:
        _store_cursor_offset(db, 0, universe_size=0)
        return [], {
            "total_pairs": total_pairs,
            "selected_pairs": 0,
            "priority_pairs": 0,
            "cursor_offset": 0,
            "next_cursor_offset": 0,
        }

    priority_market_ids = _open_position_market_ids(db) | _recent_signal_market_ids(db, now)
    priority_markets = [market for market in perp_markets if market.id in priority_market_ids]
    remaining_markets = [market for market in perp_markets if market.id not in priority_market_ids]
    priority_pairs = [
        (market, timeframe)
        for market in priority_markets
        for timeframe in valid_timeframes
    ]
    remaining_pairs = [
        (market, timeframe)
        for market in remaining_markets
        for timeframe in valid_timeframes
    ]

    selected = priority_pairs[:budget]
    remaining_budget = max(0, budget - len(selected))
    cursor = _cursor_offset(db)
    next_cursor = cursor
    if remaining_budget and remaining_pairs:
        offset = cursor % len(remaining_pairs)
        rotated = remaining_pairs[offset:] + remaining_pairs[:offset]
        selected_remaining = rotated[:remaining_budget]
        selected.extend(selected_remaining)
        next_cursor = (offset + len(selected_remaining)) % len(remaining_pairs)
    elif not remaining_pairs:
        next_cursor = 0
    _store_cursor_offset(db, next_cursor, universe_size=len(remaining_pairs))
    return selected, {
        "total_pairs": total_pairs,
        "selected_pairs": len(selected),
        "priority_pairs": min(len(priority_pairs), budget),
        "cursor_offset": cursor,
        "next_cursor_offset": next_cursor,
    }


def _fast_risk_reconcile_recent(db, *, now: float, ttl_seconds: int) -> bool:
    last_ok = _last_cycle_ok_at(db, "fast_risk_cycle")
    return last_ok is not None and now - last_ok <= ttl_seconds


def _is_market_data_breaker_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        isinstance(exc, HyperliquidTransientError)
        and "hyperliquid_market_data" in message
        and "circuit breaker" in message
    )


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
            Market.market_type == MarketType.PERP.value,
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
    rate_limit_settings = effective_hyperliquid_rate_limit_settings(db)
    logger.info(
        "worker_tick",
        active_timeframes=active_timeframes or None,
        monitoring_only_timeframes=monitoring_only_timeframes or None,
        sync_active_timeframes=sync_active_timeframes or None,
    )
    _record_heartbeat("running")
    pnl_updated = 0
    live_reconciled = 0
    live_reconcile_skipped_reason: str | None = None
    try:
        if settings.fast_risk_loop_enabled and _fast_risk_reconcile_recent(
            db,
            now=time.time(),
            ttl_seconds=rate_limit_settings["fast_risk_recent_reconcile_ttl_seconds"],
        ):
            live_reconcile_skipped_reason = "fast_risk_recent"
        else:
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
                    "live_reconcile_skipped_reason": live_reconcile_skipped_reason,
                    "live_stop_updates": 0,
                    "btc_protection_updates": btc_protection_updates,
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
                    "macro_risk": macro_risk_result,
                    "calibrated": calibrated,
                    "active_timeframes": [],
                    "sync_active_timeframes": [],
                    "market_scope": MarketType.PERP.value,
                    "rate_limit_settings": rate_limit_settings,
                    "sync_pairs_total": 0,
                    "sync_pairs_selected": 0,
                    "market_data_breaker_skipped": 0,
                },
            ))
            db.commit()
            await _notify_macro_risk_state_change(db, notifier, macro_risk_result)
            await _notify_live_position_closes(db, notifier, tick_start)
            await _notify_stop_update_failures(db, notifier, tick_start)
            await _notify_btc_macro_reversals(db, notifier, tick_start)
        finally:
            db.close()
        return 0, 0
    try:
        markets = await asyncio.to_thread(
            lambda: (
                db.query(Market)
                .filter(
                    Market.active.is_(True),
                    Market.market_type == MarketType.PERP.value,
                )
                .order_by(Market.symbol)
                .limit(500)
                .all()
            )
        )
        if settings.external_quote_enrichment_enabled:
            signal_engine.market_data.prefetch_quotes([m.base for m in markets])
        perp_oi_map = await asyncio.to_thread(fetch_perp_open_interests, settings.info_url)
        # Prefetch parallelo dei fetch HTTP candele per i soli perp del budget tick.
        # La latenza di rete è il costo dominante del ciclo: scaricare in parallelo (con
        # concorrenza limitata) invece che una coppia per volta riduce drasticamente la
        # durata del tick. La persistenza resta serializzata sulla sessione condivisa.
        sync_pairs, sync_pair_stats = select_worker_sync_pairs(
            db,
            markets,
            sync_active_timeframes,
            budget=rate_limit_settings["candle_prefetch_budget_per_tick"],
            now=tick_start,
        )
        prefetched_candles = await candle_sync.prefetch(
            db,
            sync_pairs,
            attempts=rate_limit_settings["candle_retry_attempts"],
        )
        market_data_breaker_skips = sum(
            1
            for result in prefetched_candles.values()
            if isinstance(result, Exception) and _is_market_data_breaker_error(result)
        )
        market_data_breaker_skip_logged = False
        selected_markets_by_id = {market.id: market for market, _ in sync_pairs}
        selected_markets = list(selected_markets_by_id.values())
        # Prefetch L2 books per i market selezionati prima del loop segnali: evita
        # le ~N fetch sequenziali (una per coin) dentro generate(), sostituendole con
        # un batch async a concorrenza limitata che pre-popola _cycle_l2_cache.
        if active_timeframes and selected_markets:
            await signal_engine.prefetch_l2_books(selected_markets, settings)
        synced = 0
        skipped = 0
        generated = 0
        live_stop_updates = 0
        last_heartbeat = time.monotonic()
        for market, timeframe in sync_pairs:
            # Keep heartbeat fresh during long ticks (many markets × timeframes)
            # so /health doesn't flip to "stale" while a cycle is still working.
            if time.monotonic() - last_heartbeat > 30:
                _record_heartbeat("running")
                last_heartbeat = time.monotonic()
            hl_coin = (market.metadata_json or {}).get("hl_coin") or market.symbol
            market_oi = perp_oi_map.get(str(hl_coin))
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
                if _is_market_data_breaker_error(exc):
                    if not market_data_breaker_skip_logged:
                        logger.warning(
                            "candle_sync_market_data_breaker_batch_skipped",
                            skipped_pairs=market_data_breaker_skips,
                            selected_pairs=len(sync_pairs),
                            error=str(exc),
                        )
                        market_data_breaker_skip_logged = True
                else:
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
            # Trailing reattivo per posizioni paper il cui timeframe di
            # riferimento e' piu' fine del timeframe sorgente (es. 1d -> 1h).
            await asyncio.to_thread(
                executor.position_manager.manage_paper_trailing_for_timeframe,
                db,
                market,
                timeframe,
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
            live_position = execute_signal_for_mode(
                db, executor, signal, operation_mode, market, timeframe
            )
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
                    "live_reconcile_skipped_reason": live_reconcile_skipped_reason,
                    "live_stop_updates": live_stop_updates,
                    "btc_protection_updates": btc_protection_updates,
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
                    "macro_risk": macro_risk_result,
                    "calibrated": calibrated,
                    "market_scope": MarketType.PERP.value,
                    "rate_limit_settings": rate_limit_settings,
                    "sync_pairs_total": sync_pair_stats["total_pairs"],
                    "sync_pairs_selected": sync_pair_stats["selected_pairs"],
                    "sync_priority_pairs": sync_pair_stats["priority_pairs"],
                    "sync_cursor_offset": sync_pair_stats["cursor_offset"],
                    "sync_next_cursor_offset": sync_pair_stats["next_cursor_offset"],
                    "market_data_breaker_skipped": market_data_breaker_skips,
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
        await _notify_stop_update_failures(db, notifier, tick_start)
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

        # Quando il fast_risk loop e' attivo possiede gia' il liquidation guard
        # (vedi fast_risk.py) e ne registra lo stato del ciclo: eseguirlo anche
        # qui sarebbe lavoro duplicato e pressione API extra verso Hyperliquid.
        if not settings.fast_risk_loop_enabled and scheduler.due_live_liquidation_guard(now):
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

        if not settings.fast_risk_loop_enabled and scheduler.due_unified_account_ratio_guard(now):
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
