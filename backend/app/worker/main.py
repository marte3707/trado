import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func

from app.core.config import get_settings
from app.core.enums import MarketType, OperationMode, OrderMode, SignalStatus
from app.core.logging import configure_logging
from app.core.runtime_settings import (
    effective_alert_min_score,
    effective_enabled_timeframes,
    effective_monitoring_only_timeframes,
    effective_operation_mode,
    effective_telegram_notifications_enabled,
)
from app.db.init_db import create_schema, seed_defaults
from app.db.migrations import apply_migrations
from app.db.models import AppSetting, Candle, DecisionLog, Market, MarketDataEvent, Position, Signal
from app.db.session import SessionLocal
from app.engine.btc_macro_protection import BtcMacroProtectionEngine
from app.engine.execution import OrderExecutionEngine
from app.engine.experiment_outcomes import materialize_experiment_outcomes
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
from app.services.funding_carry_alert import check_funding_carry_regime
from app.services.hlp_monitor import check_hlp_vault
from app.services.hyperliquid import (
    HyperliquidClient,
    HyperliquidTradingClient,
    HyperliquidTransientError,
    fetch_perp_open_interests,
)
from app.services.hyperliquid_rate_limit import available_hyperliquid_info_request_capacity
from app.services.leaderboard_alpha import run_leaderboard_alpha_maintenance
from app.services.live_reconciliation import reconcile_live_positions
from app.services.market_sync import MarketSyncService
from app.services.position_pnl import refresh_open_position_pnls
from app.services.the_bull_signals import poll_the_bull_signals
from app.worker.cycle_status import (
    _record_heartbeat,
    _record_worker_failure,
    run_periodic_cycle,
)
from app.worker.fast_risk import _FastRiskState, _last_cycle_ok_at, run_fast_risk_loop
from app.worker.live_close_notifications import (
    _notify_btc_macro_reversals,
    _notify_live_position_closes,
    _notify_stop_update_failures,
)
from app.worker.scheduler import (
    DATA_RETENTION_INTERVAL_SECONDS,
    DIAGNOSTIC_REPORT_INTERVAL_SECONDS,
    FUNDING_CARRY_ALERT_INTERVAL_SECONDS,
    HLP_MONITOR_INTERVAL_SECONDS,
    LEADERBOARD_ALPHA_CHECK_INTERVAL_SECONDS,
    LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS,
    UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS,
    PeriodicScheduler,
    next_daily_recap_timestamp,
)
from app.worker.timeframe_schedule import (
    active_timeframes_for_tick,
    mark_timeframes_run,
    open_position_trailing_market_ids_by_timeframe,
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


async def _check_funding_carry_regime(db, notifier: TelegramNotifier, client: HyperliquidClient) -> dict:
    return await check_funding_carry_regime(
        db,
        fetch_funding_history=client.funding_history,
        send_message=notifier.send_funding_carry_alert,
    )


async def _check_hlp_vault(db, notifier: TelegramNotifier, client: HyperliquidClient) -> dict:
    return await check_hlp_vault(
        db,
        fetch_vault_details=client.vault_details,
        send_message=notifier.send_hlp_alert,
        fetch_earn_reserves=client.borrow_lend_reserve_states,
        fetch_earn_user=client.borrow_lend_user_state,
        user_address=client.settings.hyperliquid_account_address,
    )


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
MARKET_DATA_WS_EVENT_BATCH_SIZE = 120
DEFAULT_FAST_RISK_RECENT_RECONCILE_TTL_SECONDS = 90
DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS = {
    "candle_prefetch_budget_per_tick": DEFAULT_CANDLE_PREFETCH_BUDGET,
    "candle_retry_attempts": DEFAULT_CANDLE_RETRY_ATTEMPTS,
    "fast_risk_recent_reconcile_ttl_seconds": DEFAULT_FAST_RISK_RECENT_RECONCILE_TTL_SECONDS,
    "noncritical_cooldown_seconds": 600,
    "reconcile_interval_seconds": 180,
    "uar_interval_seconds": 300,
    "liquidation_interval_seconds": 600,
    "fills_cursor_overlap_minutes": 10,
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


def signal_universe_market_ids(
    db,
    markets: list[Market],
    *,
    now: datetime,
) -> set[int]:
    """Choose liquid markets for new signals, never for position management."""
    risk = db.get(AppSetting, "risk")
    value = risk.value if risk is not None and isinstance(risk.value, dict) else {}
    try:
        max_markets = max(0, int(value.get("signal_universe_max_markets", 0)))
    except (TypeError, ValueError):
        max_markets = 0
    all_ids = {market.id for market in markets}
    if max_markets == 0 or len(all_ids) <= max_markets:
        return all_ids
    try:
        lookback_hours = max(
            1,
            int(value.get("signal_universe_liquidity_lookback_hours", 24)),
        )
    except (TypeError, ValueError):
        lookback_hours = 24
    quote_volume = func.sum(Candle.close * Candle.volume).label("quote_volume")
    rows = (
        db.query(Candle.market_id, quote_volume)
        .filter(
            Candle.market_id.in_(all_ids),
            Candle.timeframe == "1h",
            Candle.open_time >= now - timedelta(hours=lookback_hours),
        )
        .group_by(Candle.market_id)
        .order_by(quote_volume.desc())
        .limit(max_markets)
        .all()
    )
    selected = {int(row[0]) for row in rows if row[0] in all_ids}
    # No local volume evidence: retain legacy scope rather than choosing symbols
    # alphabetically and presenting that as a liquidity decision.
    return selected or all_ids


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
        "noncritical_cooldown_seconds": _int_setting(
            raw.get("noncritical_cooldown_seconds"),
            DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS["noncritical_cooldown_seconds"],
        ),
        "reconcile_interval_seconds": _int_setting(
            raw.get("reconcile_interval_seconds"),
            DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS["reconcile_interval_seconds"],
        ),
        "uar_interval_seconds": _int_setting(
            raw.get("uar_interval_seconds"),
            DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS["uar_interval_seconds"],
        ),
        "liquidation_interval_seconds": _int_setting(
            raw.get("liquidation_interval_seconds"),
            DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS["liquidation_interval_seconds"],
        ),
        "fills_cursor_overlap_minutes": _int_setting(
            raw.get("fills_cursor_overlap_minutes"),
            DEFAULT_HYPERLIQUID_RATE_LIMIT_SETTINGS["fills_cursor_overlap_minutes"],
        ),
    }


def _open_position_market_ids(db, *, mode: str | None = None) -> set[int]:
    query = db.query(Position.market_id).filter(Position.status == "open")
    if mode is None:
        query = query.filter(Position.mode.in_([OrderMode.LIVE.value, OrderMode.PAPER.value]))
    else:
        query = query.filter(Position.mode == mode)
    rows = query.distinct().all()
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


def _pending_market_data_events(db) -> list[MarketDataEvent]:
    # The DB queue is authoritative. The status snapshot is updated periodically
    # and may briefly report zero while a newly persisted close event is pending.
    rows = (
        db.query(MarketDataEvent)
        .filter(MarketDataEvent.processed_at.is_(None))
        .order_by(MarketDataEvent.candle_open_time.desc())
        .limit(MARKET_DATA_WS_EVENT_BATCH_SIZE)
        .all()
    )
    newest_by_pair: dict[tuple[int, str], MarketDataEvent] = {}
    for row in rows:
        newest_by_pair.setdefault((row.market_id, row.timeframe), row)
    return list(newest_by_pair.values())


def _mark_market_data_event_processed(db, event: MarketDataEvent) -> None:
    processed_at = datetime.now(UTC)
    (
        db.query(MarketDataEvent)
        .filter(
            MarketDataEvent.market_id == event.market_id,
            MarketDataEvent.timeframe == event.timeframe,
            MarketDataEvent.processed_at.is_(None),
            MarketDataEvent.candle_open_time <= event.candle_open_time,
        )
        .update({MarketDataEvent.processed_at: processed_at}, synchronize_session=False)
    )


def _cursor_offsets(db) -> tuple[int, int]:
    setting = db.get(AppSetting, WORKER_MARKET_SYNC_CURSOR_KEY)
    raw = setting.value if setting is not None else {}
    raw_offset = raw.get("offset", 0) if isinstance(raw, dict) else raw
    raw_priority_offset = raw.get("priority_offset", 0) if isinstance(raw, dict) else 0
    return (
        _int_setting(raw_offset, 0, minimum=0),
        _int_setting(raw_priority_offset, 0, minimum=0),
    )


def _store_cursor_offsets(
    db,
    offset: int,
    priority_offset: int,
    *,
    universe_size: int,
    priority_universe_size: int,
) -> None:
    value = {
        "offset": max(0, int(offset)),
        "priority_offset": max(0, int(priority_offset)),
        "universe_size": max(0, int(universe_size)),
        "priority_universe_size": max(0, int(priority_universe_size)),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    setting = db.get(AppSetting, WORKER_MARKET_SYNC_CURSOR_KEY)
    if setting is None:
        db.add(AppSetting(key=WORKER_MARKET_SYNC_CURSOR_KEY, value=value))
    else:
        setting.value = value


def _take_rotated_pairs(
    pairs: list[tuple[Market, str]],
    cursor: int,
    limit: int,
) -> tuple[list[tuple[Market, str]], int]:
    if not pairs or limit <= 0:
        return [], 0 if not pairs else cursor % len(pairs)
    offset = cursor % len(pairs)
    rotated = pairs[offset:] + pairs[:offset]
    selected = rotated[:limit]
    return selected, (offset + len(selected)) % len(pairs)


def select_worker_sync_pairs(
    db,
    markets: list[Market],
    timeframes: list[str],
    *,
    budget: int,
    now: datetime,
    latest_open_by_pair: dict[tuple[int, str], datetime] | None = None,
    restricted_market_ids_by_timeframe: dict[str, set[int]] | None = None,
    priority_pair_keys: set[tuple[int, str]] | None = None,
) -> tuple[list[tuple[Market, str]], dict[str, int]]:
    valid_timeframes = [timeframe for timeframe in timeframes if timeframe in TIMEFRAME_MS]
    perp_markets = [
        market
        for market in markets
        if market.active and market.market_type == MarketType.PERP.value
    ]
    restrictions = restricted_market_ids_by_timeframe or {}

    def eligible(market: Market, timeframe: str) -> bool:
        allowed_market_ids = restrictions.get(timeframe)
        if allowed_market_ids is not None and market.id not in allowed_market_ids:
            return False
        if latest_open_by_pair is None:
            return True
        interval_ms = TIMEFRAME_MS[timeframe]
        latest_closed_ms = (int(now.timestamp() * 1000) // interval_ms - 1) * interval_ms
        latest_closed = datetime.fromtimestamp(latest_closed_ms / 1000, tz=UTC)
        latest_open = latest_open_by_pair.get((market.id, timeframe))
        return latest_open is None or latest_open < latest_closed

    total_pairs = sum(
        1
        for market in perp_markets
        for timeframe in valid_timeframes
        if eligible(market, timeframe)
    )
    cursor, priority_cursor = _cursor_offsets(db)
    if not perp_markets or not valid_timeframes:
        _store_cursor_offsets(
            db,
            0,
            0,
            universe_size=0,
            priority_universe_size=0,
        )
        return [], {
            "total_pairs": total_pairs,
            "selected_pairs": 0,
            "priority_pairs": 0,
            "cursor_offset": 0,
            "next_cursor_offset": 0,
        }
    if budget <= 0:
        # A temporarily exhausted rate-limit budget must not rewind fairness.
        return [], {
            "total_pairs": total_pairs,
            "selected_pairs": 0,
            "priority_pairs": 0,
            "cursor_offset": cursor,
            "next_cursor_offset": cursor,
        }

    live_market_ids = _open_position_market_ids(db, mode=OrderMode.LIVE.value)
    open_market_ids = _open_position_market_ids(db)
    recent_market_ids = _recent_signal_market_ids(db, now)
    priority_market_ids = open_market_ids | recent_market_ids
    live_markets = [market for market in perp_markets if market.id in live_market_ids]
    other_open_markets = [
        market
        for market in perp_markets
        if market.id in open_market_ids and market.id not in live_market_ids
    ]
    recent_markets = [
        market
        for market in perp_markets
        if market.id in recent_market_ids and market.id not in open_market_ids
    ]
    priority_markets = [*live_markets, *other_open_markets, *recent_markets]
    remaining_markets = [market for market in perp_markets if market.id not in priority_market_ids]
    explicit_priority_keys = priority_pair_keys or set()
    explicit_priority_pairs = [
        (market, timeframe)
        for market in perp_markets
        for timeframe in valid_timeframes
        if (market.id, timeframe) in explicit_priority_keys and eligible(market, timeframe)
    ]
    regular_priority_pairs = [
        (market, timeframe)
        for market in priority_markets
        for timeframe in valid_timeframes
        if (market.id, timeframe) not in explicit_priority_keys and eligible(market, timeframe)
    ]
    priority_pairs = [*explicit_priority_pairs, *regular_priority_pairs]
    remaining_pairs = [
        (market, timeframe)
        for market in remaining_markets
        for timeframe in valid_timeframes
        if (market.id, timeframe) not in explicit_priority_keys and eligible(market, timeframe)
    ]

    if priority_pairs and remaining_pairs and budget >= 2:
        priority_budget = (budget + 1) // 2
        remaining_budget = budget - priority_budget
    elif priority_pairs:
        priority_budget = budget
        remaining_budget = 0
    else:
        priority_budget = 0
        remaining_budget = budget

    selected_priority, next_priority_cursor = _take_rotated_pairs(
        priority_pairs,
        priority_cursor,
        priority_budget,
    )
    selected_remaining, next_cursor = _take_rotated_pairs(
        remaining_pairs,
        cursor,
        remaining_budget,
    )
    selected = [*selected_priority, *selected_remaining]

    unused_budget = budget - len(selected)
    if unused_budget > 0 and len(selected_priority) < len(priority_pairs):
        extra, next_priority_cursor = _take_rotated_pairs(
            priority_pairs,
            next_priority_cursor,
            min(unused_budget, len(priority_pairs) - len(selected_priority)),
        )
        selected.extend(extra)
        selected_priority.extend(extra)
        unused_budget -= len(extra)
    if unused_budget > 0 and len(selected_remaining) < len(remaining_pairs):
        extra, next_cursor = _take_rotated_pairs(
            remaining_pairs,
            next_cursor,
            min(unused_budget, len(remaining_pairs) - len(selected_remaining)),
        )
        selected.extend(extra)
        selected_remaining.extend(extra)

    _store_cursor_offsets(
        db,
        next_cursor,
        next_priority_cursor,
        universe_size=len(remaining_pairs),
        priority_universe_size=len(priority_pairs),
    )
    return selected, {
        "total_pairs": total_pairs,
        "selected_pairs": len(selected),
        "priority_pairs": len(selected_priority),
        "cursor_offset": cursor,
        "next_cursor_offset": next_cursor,
    }


def _monitoring_sync_scope(
    markets: list[Market],
    sync_active_timeframes: list[str],
    monitoring_only_timeframes: list[str],
    active_timeframes: list[str],
    trailing_market_ids_by_timeframe: dict[str, set[int]],
) -> tuple[dict[str, set[int]], set[tuple[int, str]]]:
    monitoring_market_ids = {
        market.id
        for market in markets
        if market.base in {"BTC", "ETH"}
        or bool((market.metadata_json or {}).get("monitoring_only"))
    }
    monitoring_only_set = set(monitoring_only_timeframes)
    active_set = set(active_timeframes)
    restricted_market_ids_by_timeframe: dict[str, set[int]] = {}
    priority_pair_keys: set[tuple[int, str]] = set()

    for timeframe in sync_active_timeframes:
        if timeframe not in monitoring_only_set:
            continue
        allowed_market_ids = set(monitoring_market_ids)
        allowed_market_ids.update(trailing_market_ids_by_timeframe.get(timeframe, set()))
        if timeframe not in active_set:
            restricted_market_ids_by_timeframe[timeframe] = allowed_market_ids
        priority_pair_keys.update((market_id, timeframe) for market_id in allowed_market_ids)

    return restricted_market_ids_by_timeframe, priority_pair_keys


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
    notifier: TelegramNotifier,
    executor: OrderExecutionEngine,
    btc_protection: BtcMacroProtectionEngine | None = None,
) -> tuple[int, int]:
    tick_start = datetime.now(UTC)
    settings = get_settings()
    db = SessionLocal()
    notifier.enabled = effective_telegram_notifications_enabled(db)
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
    pending_market_data_events = _pending_market_data_events(db)
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
    portfolio_profit_lock_result: dict = {"status": "not_run", "updated": 0}
    try:
        if settings.fast_risk_loop_enabled:
            live_reconcile_skipped_reason = (
                "fast_risk_recent"
                if _fast_risk_reconcile_recent(
                    db,
                    now=time.time(),
                    ttl_seconds=rate_limit_settings["fast_risk_recent_reconcile_ttl_seconds"],
                )
                else "fast_risk_owner"
            )
        else:
            live_reconciled = await asyncio.to_thread(reconcile_live_positions, db)
        pnl_updated = await refresh_open_position_pnls(db)
        portfolio_profit_lock_result = await asyncio.to_thread(
            executor.position_manager.manage_live_portfolio_profit_lock,
            db,
        )
    except HyperliquidTransientError as exc:
        logger.warning("position_pnl_refresh_transient_failed", error=str(exc))
        db.rollback()
    except Exception as exc:
        logger.exception("position_pnl_refresh_failed", error=str(exc))
        db.rollback()
    stale_market_data_result = await asyncio.to_thread(
        executor.apply_stale_market_data_guard,
        db,
    )
    closed = int(stale_market_data_result.get("paper_closed", 0))
    closed += await asyncio.to_thread(
        reconcile_open_paper_positions, db, executor, managed_sync_timeframes
    )
    time_stop_result = await asyncio.to_thread(executor.apply_time_stops, db)
    closed += int(time_stop_result.get("paper_closed", 0))
    portfolio_breaker_result = executor.portfolio_guard.update_circuit_breaker(db)
    db.commit()
    experiment_outcomes = {"candidates": 0, "created": 0, "skipped": 0}
    try:
        experiment_outcomes = await asyncio.to_thread(
            materialize_experiment_outcomes,
            db,
            taker_fee_rate=settings.live_taker_fee_rate,
        )
    except Exception as exc:
        logger.exception("experiment_outcome_materialization_failed", error=str(exc))
        db.rollback()
    btc_protection_updates = 0
    the_bull_result: dict = {
        "status": "not_run",
        "processed": 0,
        "opened": 0,
        "closed": 0,
    }
    try:
        the_bull_result = await poll_the_bull_signals(db, settings, executor)
        closed += int(the_bull_result.get("closed", 0))
        if int(the_bull_result.get("opened", 0)) > 0:
            recent_external_live = (
                db.query(Position)
                .filter(
                    Position.mode == OrderMode.LIVE.value,
                    Position.status == "open",
                    Position.opened_at >= tick_start,
                )
                .all()
            )
            for position in recent_external_live:
                source = (position.metadata_json or {}).get("external_source")
                if not isinstance(source, dict) or source.get("provider") != "the_bull":
                    continue
                try:
                    await notifier.send_live_position_opened(position)
                except Exception:
                    logger.exception(
                        "telegram_the_bull_live_open_notify_failed",
                        position_id=position.id,
                    )
    except Exception as exc:
        logger.exception("the_bull_cycle_failed", error=str(exc))
        db.rollback()
        the_bull_result = {
            "status": "error",
            "processed": 0,
            "opened": 0,
            "closed": 0,
            "error": str(exc)[:500],
        }
    # Nota: btc_protection.run() viene invocato DOPO il loop di generazione segnali
    # (più sotto) — non qui — perché in reverse_mode deve poter agire sui signal flip
    # generati nello stesso ciclo. La protezione legacy live-only era idempotente
    # rispetto all'ordine; con reverse_mode l'ordine conta.
    if not active_timeframes and not sync_active_timeframes and not pending_market_data_events:
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
                    "experiment_outcomes": experiment_outcomes,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_reconcile_skipped_reason": live_reconcile_skipped_reason,
                    "portfolio_profit_lock": portfolio_profit_lock_result,
                    "live_stop_updates": 0,
                    "btc_protection_updates": btc_protection_updates,
                    "the_bull": the_bull_result,
                    "stale_market_data_paper_closed": stale_market_data_result.get(
                        "paper_closed", 0
                    ),
                    "stale_market_data_paper_alerted": stale_market_data_result.get(
                        "paper_alerted", 0
                    ),
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
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
        signal_market_ids = await asyncio.to_thread(
            signal_universe_market_ids,
            db,
            markets,
            now=tick_start,
        )
        perp_oi_map = await asyncio.to_thread(fetch_perp_open_interests, settings.info_url)
        # Prefetch parallelo dei fetch HTTP candele per i soli perp del budget tick.
        # La latenza di rete è il costo dominante del ciclo: scaricare in parallelo (con
        # concorrenza limitata) invece che una coppia per volta riduce drasticamente la
        # durata del tick. La persistenza resta serializzata sulla sessione condivisa.
        market_by_id = {market.id: market for market in markets}
        event_by_pair = {
            (event.market_id, event.timeframe): event
            for event in pending_market_data_events
            if event.market_id in market_by_id and event.timeframe in managed_sync_timeframes
        }
        event_pairs = [
            (market_by_id[market_id], timeframe)
            for market_id, timeframe in event_by_pair
        ]
        event_timeframes = {timeframe for _, timeframe in event_pairs}
        signal_timeframes = set(active_timeframes) | (
            event_timeframes & set(enabled_timeframes)
        )
        trailing_market_ids_by_timeframe = await asyncio.to_thread(
            open_position_trailing_market_ids_by_timeframe,
            db,
        )
        (
            restricted_market_ids_by_timeframe,
            monitoring_priority_pair_keys,
        ) = _monitoring_sync_scope(
            markets,
            sync_active_timeframes,
            monitoring_only_timeframes,
            active_timeframes,
            trailing_market_ids_by_timeframe,
        )
        rest_sync_timeframes = list(
            dict.fromkeys([*sync_active_timeframes, *event_timeframes])
        )
        latest_open_by_pair = await asyncio.to_thread(
            candle_sync.latest_open_map,
            db,
            [market.id for market in markets],
            rest_sync_timeframes,
        )
        available_candle_requests = available_hyperliquid_info_request_capacity(
            db,
            request_type="candleSnapshot",
        )
        event_l2_markets = {
            market.id
            for market, timeframe in event_pairs
            if timeframe in signal_timeframes and market.id in signal_market_ids
        }
        available_candle_requests = max(
            0,
            available_candle_requests - len(event_l2_markets),
        )
        if active_timeframes:
            # Each signal market may also need one l2Book request this minute.
            available_candle_requests //= 2
        effective_candle_budget = min(
            rate_limit_settings["candle_prefetch_budget_per_tick"],
            available_candle_requests,
        )
        signal_priority_pair_keys = {
            (market_id, timeframe)
            for market_id in signal_market_ids
            for timeframe in signal_timeframes
        }
        rest_pairs, sync_pair_stats = select_worker_sync_pairs(
            db,
            markets,
            sync_active_timeframes,
            budget=effective_candle_budget,
            now=tick_start,
            latest_open_by_pair=latest_open_by_pair,
            restricted_market_ids_by_timeframe=restricted_market_ids_by_timeframe,
            priority_pair_keys=monitoring_priority_pair_keys | signal_priority_pair_keys,
        )
        event_pair_keys = {(market.id, timeframe) for market, timeframe in event_pairs}
        rest_pairs = [
            pair for pair in rest_pairs if (pair[0].id, pair[1]) not in event_pair_keys
        ]
        sync_pairs = [*event_pairs, *rest_pairs]
        prefetched_candles = await candle_sync.prefetch(
            db,
            rest_pairs,
            attempts=rate_limit_settings["candle_retry_attempts"],
        )
        market_data_breaker_skips = sum(
            1
            for result in prefetched_candles.values()
            if isinstance(result, Exception) and _is_market_data_breaker_error(result)
        )
        market_data_breaker_skip_logged = False
        # L2 is loaded lazily in SignalEngine.generate(), after its duplicate
        # check. Prefetching here also queried books for candles that already
        # had a stored signal, consuming shared /info capacity for no decision.
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
            market_data_event = event_by_pair.get((market.id, timeframe))
            try:
                if market_data_event is None:
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
                if market_data_event is not None:
                    _mark_market_data_event_processed(db, market_data_event)
                    db.commit()
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
            if timeframe not in signal_timeframes:
                if market_data_event is not None:
                    _mark_market_data_event_processed(db, market_data_event)
                    db.commit()
                continue
            if market.id not in signal_market_ids:
                if market_data_event is not None:
                    _mark_market_data_event_processed(db, market_data_event)
                    db.commit()
                continue
            signal = await asyncio.to_thread(signal_engine.generate, db, market, timeframe)
            if market_data_event is not None:
                _mark_market_data_event_processed(db, market_data_event)
                db.commit()
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
        try:
            latest_outcomes = await asyncio.to_thread(
                materialize_experiment_outcomes,
                db,
                taker_fee_rate=settings.live_taker_fee_rate,
            )
            for key in experiment_outcomes:
                experiment_outcomes[key] += latest_outcomes[key]
        except Exception as exc:
            logger.exception("experiment_outcome_materialization_failed", error=str(exc))
            db.rollback()
        completed_sync_backlog = (
            skipped == 0
            and sync_pair_stats["selected_pairs"] >= sync_pair_stats["total_pairs"]
        )
        if completed_sync_backlog:
            mark_timeframes_run(sync_active_timeframes, now_ms)
        db.add(
            DecisionLog(
                event_type="worker_cycle",
                message="Worker cycle completed",
                data={
                    "markets": len(markets),
                    "signal_universe_markets": len(signal_market_ids),
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
                    "experiment_outcomes": experiment_outcomes,
                    "pnl_updated": pnl_updated,
                    "live_reconciled": live_reconciled,
                    "live_reconcile_skipped_reason": live_reconcile_skipped_reason,
                    "portfolio_profit_lock": portfolio_profit_lock_result,
                    "live_stop_updates": live_stop_updates,
                    "btc_protection_updates": btc_protection_updates,
                    "the_bull": the_bull_result,
                    "stale_market_data_paper_closed": stale_market_data_result.get(
                        "paper_closed", 0
                    ),
                    "stale_market_data_paper_alerted": stale_market_data_result.get(
                        "paper_alerted", 0
                    ),
                    "time_stop_paper_closed": time_stop_result.get("paper_closed", 0),
                    "time_stop_live_alerted": time_stop_result.get("live_alerted", 0),
                    "portfolio_circuit_breaker": portfolio_breaker_result,
                    "market_scope": MarketType.PERP.value,
                    "rate_limit_settings": rate_limit_settings,
                    "sync_pairs_total": sync_pair_stats["total_pairs"],
                    "sync_pairs_selected": sync_pair_stats["selected_pairs"],
                    "ws_events_selected": len(event_pairs),
                    "sync_candle_budget": effective_candle_budget,
                    "sync_backlog_completed": completed_sync_backlog,
                    "sync_priority_pairs": sync_pair_stats["priority_pairs"],
                    "sync_cursor_offset": sync_pair_stats["cursor_offset"],
                    "sync_next_cursor_offset": sync_pair_stats["next_cursor_offset"],
                    "market_data_breaker_skipped": market_data_breaker_skips,
                    # Aggregato dei motivi per cui i segnali non sono stati emessi —
                    # in produzione gli eventi debug per skip sono filtrati, qui
                    # restano visibili per debug "perché non genero?".
                    "skip_reasons": signal_engine.cycle_skip_counts(),
                    # Each market/source candle is counted once per worker
                    # process; persisted outcomes remain the trade evidence.
                    "candidate_funnel": signal_engine.cycle_candidate_funnel(),
                },
            )
        )
        db.commit()
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
    notifier = TelegramNotifier(settings)
    info_client = HyperliquidClient(settings)
    executor = OrderExecutionEngine(settings)
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
        notifier.enabled = effective_telegram_notifications_enabled(startup_db)
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
                        notifier,
                        executor,
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

        if scheduler.due_funding_carry_alert(now):
            await run_periodic_cycle(
                cycle_id="funding_carry_alert_cycle",
                label="Funding Carry Alert Cycle",
                expected_interval_seconds=FUNDING_CARRY_ALERT_INTERVAL_SECONDS,
                work=lambda db: _check_funding_carry_regime(db, notifier, info_client),
                ok_message="Funding carry regime checked",
                failed_message="Funding carry regime check failed",
                ok_status=lambda result: (
                    "skipped"
                    if (result or {}).get("status") in {"skipped", "disabled"}
                    else "ok"
                ),
            )

        if scheduler.due_hlp_monitor(now):
            await run_periodic_cycle(
                cycle_id="hlp_monitor_cycle",
                label="HLP Vault Monitor Cycle",
                expected_interval_seconds=HLP_MONITOR_INTERVAL_SECONDS,
                work=lambda db: _check_hlp_vault(db, notifier, info_client),
                ok_message="HLP vault checked",
                failed_message="HLP vault check failed",
                ok_status=lambda result: (
                    "skipped"
                    if (result or {}).get("status") in {"skipped", "disabled"}
                    else "ok"
                ),
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

        if scheduler.due_leaderboard_alpha(now):
            await run_periodic_cycle(
                cycle_id="leaderboard_alpha_cycle",
                label="Leaderboard Alpha Cycle",
                expected_interval_seconds=LEADERBOARD_ALPHA_CHECK_INTERVAL_SECONDS,
                work=lambda db: run_leaderboard_alpha_maintenance(db),
                ok_message="Leaderboard alpha cycle completed",
                failed_message="Leaderboard alpha cycle failed",
                ok_status=lambda result: (
                    "skipped"
                    if (result or {}).get("status") in {"skipped", "rate_limited", "disabled"}
                    else "ok"
                ),
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
