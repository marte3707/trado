import asyncio
import dataclasses
import time
from datetime import datetime, timezone
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
from app.engine.signal_engine import SignalEngine
from app.notifications.diagnostic_report import DiagnosticReporter
from app.notifications.telegram import TelegramNotifier
from app.services.candle_sync import CandleSyncService, TIMEFRAME_MS
from app.services.hyperliquid import HyperliquidTradingClient, HyperliquidTransientError, fetch_perp_open_interests
from app.services.live_reconciliation import reconcile_live_positions
from app.services.market_sync import MarketSyncService
from app.services.position_pnl import refresh_open_position_pnls
from app.services.unified_account_ratio import (
    DEFAULT_UAR_RISK_SETTINGS,
    calculate_unified_account_ratio,
    decimal_or_none,
    free_spot_usdc_from_balances,
)

logger = structlog.get_logger()
_LAST_TIMEFRAME_RUN_MS: dict[str, int] = {}
_TICK_LOCK = asyncio.Lock()

_RECAP_INTERVAL_SECONDS = 10800  # ogni 3 ore
_DIAGNOSTIC_REPORT_INTERVAL_SECONDS = 86400  # ogni 24h
_LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS = 10800  # ogni 3 ore
_UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS = 60
_ERROR_NOTIFY_COOLDOWN_SECONDS = 1800  # max 1 notifica errore ogni 30 min
_next_recap_at: float = 0.0
_next_diagnostic_at: float = 0.0
_next_live_liquidation_guard_at: float = 0.0
_next_unified_account_ratio_guard_at: float = 0.0
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
            source_signal_id = metadata.get("source_signal_id")
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
    try:
        return int(value["last_trade_count"])
    except (KeyError, TypeError, ValueError):
        return None


def _starvation_soft_threshold(db) -> int:
    setting = db.get(AppSetting, "auto_calibration")
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    try:
        return int(value.get("starvation_soft_cycles", 10))
    except (TypeError, ValueError):
        return 10


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
    return _consecutive_zero_signal_cycles_from_log(db) >= _starvation_soft_threshold(db)


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
    now = datetime.now(timezone.utc).isoformat()
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
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason=order_type,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size,
                    step_pnl=step_pnl,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
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
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=event.data.get("side") or position.side,
                    reason=reason,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    remaining_size=remaining_size if is_partial else None,
                    step_pnl=step_pnl if is_partial else None,
                    total_pnl=total_pnl,
                    is_partial=is_partial,
                )

            elif event.event_type == "live_position_reconciled_closed":
                if event.position_id in positions_with_fills:
                    continue
                await notifier.send_live_position_closed(
                    symbol=market.symbol,
                    side=position.side,
                    reason="reconciliazione hl",
                    entry_price=position.entry_price,
                    exit_price=None,
                    remaining_size=None,
                    step_pnl=None,
                    total_pnl=Decimal(str(position.realized_pnl or 0)),
                    is_partial=False,
                )

        except Exception:
            logger.warning(
                "telegram_live_close_notify_failed",
                event_type=event.event_type,
                position_id=event.position_id,
            )


async def run_once(
    signal_engine: SignalEngine,
    candle_sync: CandleSyncService,
    calibration_engine: AutoCalibrationEngine,
    notifier: TelegramNotifier,
    executor: OrderExecutionEngine,
    btc_protection: BtcMacroProtectionEngine | None = None,
) -> tuple[int, int]:
    tick_start = datetime.now(timezone.utc)
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
                    "calibrated": calibrated,
                    "active_timeframes": [],
                    "paper_active_timeframes": [],
                },
            ))
            db.commit()
            await _notify_live_position_closes(db, notifier, tick_start)
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
                try:
                    await candle_sync.sync_market(db, market, timeframe, open_interest=market_oi)
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
                if timeframe not in active_timeframes:
                    await asyncio.sleep(0.01)
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
                live_position = execute_signal_for_mode(db, executor, signal, operation_mode, market, timeframe)
                if live_position is not None:
                    try:
                        await notifier.send_live_position_opened(live_position)
                    except Exception:
                        logger.exception("telegram_live_open_notify_failed")
                await asyncio.sleep(0.05)
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
                    "calibrated": calibrated,
                    # Aggregato dei motivi per cui i segnali non sono stati emessi —
                    # in produzione gli eventi debug per skip sono filtrati, qui
                    # restano visibili per debug "perché non genero?".
                    "skip_reasons": signal_engine.cycle_skip_counts(),
                },
            )
        )
        db.commit()
        await _notify_live_position_closes(db, notifier, tick_start)
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
    btc_protection = BtcMacroProtectionEngine(settings)
    _record_heartbeat("starting")

    global _next_recap_at, _next_diagnostic_at, _next_live_liquidation_guard_at, _next_unified_account_ratio_guard_at, _last_error_notify_at
    _next_recap_at = time.time() + _RECAP_INTERVAL_SECONDS
    _next_diagnostic_at = time.time() + _DIAGNOSTIC_REPORT_INTERVAL_SECONDS
    _next_live_liquidation_guard_at = 0.0
    _next_unified_account_ratio_guard_at = 0.0
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
                        signal_engine, candle_sync, calibration_engine, notifier, executor, btc_protection
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

        if now >= _next_diagnostic_at:
            _next_diagnostic_at = now + _DIAGNOSTIC_REPORT_INTERVAL_SECONDS
            diag_db = SessionLocal()
            try:
                await DiagnosticReporter().send(diag_db, notifier)
            except Exception:
                logger.exception("telegram_diagnostic_report_failed")
            finally:
                diag_db.close()

        if now >= _next_live_liquidation_guard_at:
            _next_live_liquidation_guard_at = now + _LIVE_LIQUIDATION_GUARD_INTERVAL_SECONDS
            liq_db = SessionLocal()
            try:
                result = await asyncio.to_thread(
                    executor.position_manager.audit_live_positions_against_liquidation,
                    liq_db,
                )
                logger.info("live_liquidation_guard_cycle_completed", **result)
            except Exception as exc:
                logger.exception("live_liquidation_guard_cycle_failed", error=str(exc))
                liq_db.rollback()
            finally:
                liq_db.close()

        if now >= _next_unified_account_ratio_guard_at:
            _next_unified_account_ratio_guard_at = now + _UNIFIED_ACCOUNT_RATIO_GUARD_INTERVAL_SECONDS
            uar_db = SessionLocal()
            try:
                result = await asyncio.to_thread(
                    audit_unified_account_ratio_guard,
                    uar_db,
                    executor,
                )
                logger.info("unified_account_ratio_guard_cycle_completed", **result)
            except Exception as exc:
                logger.exception("unified_account_ratio_guard_cycle_failed", error=str(exc))
                uar_db.rollback()
            finally:
                uar_db.close()

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
