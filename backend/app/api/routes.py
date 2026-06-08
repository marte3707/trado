import os
import shlex
import subprocess
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path as FilePath
from time import perf_counter
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.runtime_settings import (
    effective_live_execution_settings,
    effective_live_trading_enabled,
    effective_operation_mode,
)
from app.db.init_db import seed_timeframe
from app.db.models import (
    AccountSnapshot,
    Alert,
    AppSetting,
    BotUser,
    Candle,
    DecisionLog,
    Indicator,
    Market,
    Order,
    Position,
    PushSubscription,
    Signal,
    StrategyConfig,
)
from app.db.session import get_db
from app.engine.auto_tuner import AutoTuner
from app.engine.calibration import AutoCalibrationEngine, serialize_calibration
from app.engine.diagnostics import build_diagnostics_report, build_equity_slope_diagnostics
from app.engine.edge_profile import build_outcome_profile
from app.engine.execution import OrderExecutionEngine
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats
from app.engine.score_calibration import (
    DEFAULT_LOOKBACK as CALIBRATION_DEFAULT_LOOKBACK,
    build_calibration_table_from_db,
    cached_calibration_table,
    serialize_table,
)
from app.schemas import CandleOut, IndicatorOut, MarketOut, PushSubscribePayload, PushUnsubscribePayload, SettingsPatch, SignalOut, StrategyPatch
from app.services.candle_sync import TIMEFRAME_MS
from app.services.coinmarketcap import CoinMarketCapClient
from app.services.hyperliquid import HyperliquidClient, HyperliquidError, HyperliquidTradingClient, parse_hl_candle
from app.services.market_dashboard import MarketDashboardService
from app.services.market_sync import MarketSyncService
from app.services.position_pnl import calculate_unrealized_pnl as _calculate_unrealized_pnl
from app.services.trade_cost import aggregate_live_costs, serialize_live_cost_report
from app.services.trade_metrics import build_trade_metrics
from app.services.unified_account_ratio import (
    DEFAULT_UAR_RISK_SETTINGS,
    calculate_unified_account_ratio,
    decimal_or_none,
    quantized_decimal,
)
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.registry import strategy_registry

router = APIRouter(prefix="/api")
_market_data_client: CoinMarketCapClient | None = None
_market_dashboard_service: MarketDashboardService | None = None
_logger = structlog.get_logger()


def _log_and_redact(event: str, exc: Exception) -> str:
    """Log the full exception server-side and return a safe error_id to embed
    in the API response. Callers never echo exception messages anymore."""
    error_id = uuid.uuid4().hex[:12]
    _logger.exception(event, error_id=error_id, error=str(exc))
    return error_id

# Beyond this age, Position.unrealized_pnl is considered stale and the API
# recomputes it on the fly from the latest candle, instead of trusting the
# worker-written value.
PNL_STALENESS = timedelta(minutes=5)
POSITION_HISTORY_LIMIT = 250
ORDER_HISTORY_LIMIT = 250
EXIT_ORDER_TYPES = ("tp1", "tp2", "tp3", "take_profit", "stop_loss", "manual")
HISTORY_ORDER_TYPES = (
    *EXIT_ORDER_TYPES,
    "time_stop",
    "btc_macro_protection_loss_exit",
    "liquidation_guard_failed",
)

_TRADE_MANAGEMENT_GUIDE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Trado · Trade Management Guide</title>
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #0b1411; color: #ecf4ef; }
      main { max-width: 860px; margin: 0 auto; padding: 40px 20px 72px; line-height: 1.6; }
      h1, h2, h3 { line-height: 1.2; }
      h1 { font-size: 2rem; margin-bottom: 0.25rem; }
      h2 { margin-top: 2rem; border-top: 1px solid #214036; padding-top: 1.5rem; }
      .muted { color: #9bb5ab; }
      .card { background: #11201b; border: 1px solid #214036; border-radius: 14px; padding: 18px 20px; margin-top: 16px; }
      a { color: #57d6a3; }
      ul { padding-left: 1.2rem; }
      code { background: #173028; padding: 0.1rem 0.35rem; border-radius: 6px; }
    </style>
  </head>
  <body>
    <main>
      <h1>Trade Management Guide</h1>
      <p class="muted">Italiano + English glossary for the terms used in Trado Telegram messages.</p>

      <h2 id="trailing-stop">Trailing Stop</h2>
      <div class="card">
        <p><strong>IT:</strong> Uno stop loss che viene spostato a favore della posizione mentre il trade va in profitto, per proteggere parte del guadagno.</p>
        <p><strong>EN:</strong> A stop loss that moves in your favor as the trade becomes profitable, helping protect part of the gains.</p>
      </div>

      <h2 id="breakeven">Breakeven</h2>
      <div class="card">
        <p><strong>IT:</strong> Spostare lo stop in corrispondenza del prezzo di ingresso, così l'uscita non genera più perdita netta sul trade.</p>
        <p><strong>EN:</strong> Moving the stop to the entry price so the trade can no longer produce a net loss.</p>
      </div>

      <h2 id="risk-reward">Risk / Reward</h2>
      <div class="card">
        <p><strong>IT:</strong> Rapporto tra perdita potenziale e profitto atteso. Un <code>R/R 2.0</code> indica che il target teorico vale circa il doppio del rischio iniziale.</p>
        <p><strong>EN:</strong> The ratio between potential loss and expected profit. A <code>R/R 2.0</code> means the theoretical target is about twice the initial risk.</p>
      </div>

      <h2 id="runner">Runner</h2>
      <div class="card">
        <p><strong>IT:</strong> La parte residua della posizione lasciata aperta dopo i take profit parziali, con l'obiettivo di sfruttare ulteriormente il trend.</p>
        <p><strong>EN:</strong> The remaining part of the position left open after partial take profits, aiming to capture more of the trend.</p>
      </div>

      <h2 id="stop-loss">Stop Loss</h2>
      <div class="card">
        <p><strong>IT:</strong> Livello di uscita che limita la perdita massima pianificata se il trade si muove contro la posizione.</p>
        <p><strong>EN:</strong> Exit level designed to cap the planned maximum loss if the trade moves against the position.</p>
      </div>

      <h2 id="martingale">Martingale</h2>
      <div class="card">
        <p><strong>IT:</strong> Strategia che aumenta la size dopo una perdita per cercare di recuperarla. In Trado non viene usata.</p>
        <p><strong>EN:</strong> A strategy that increases position size after a loss to try to recover it. Trado does not use it.</p>
      </div>

      <h2 id="averaging-down">Averaging Down</h2>
      <div class="card">
        <p><strong>IT:</strong> Aggiungere size a una posizione in perdita per abbassare il prezzo medio di ingresso. In Trado non viene usato automaticamente.</p>
        <p><strong>EN:</strong> Adding size to a losing position to lower the average entry price. Trado does not do this automatically.</p>
      </div>
    </main>
  </body>
</html>
"""


def market_data_client() -> CoinMarketCapClient:
    global _market_data_client
    if _market_data_client is None:
        _market_data_client = CoinMarketCapClient(get_settings())
    return _market_data_client


def market_dashboard_service() -> MarketDashboardService:
    global _market_dashboard_service
    if _market_dashboard_service is None:
        _market_dashboard_service = MarketDashboardService(get_settings())
    return _market_dashboard_service


def _source_timeframe(position: Position) -> str | None:
    raw = (position.metadata_json or {}).get("source_timeframe")
    return raw if isinstance(raw, str) and raw else None


def _latest_candle_for_position(db: Session, position: Position) -> Candle | None:
    query = db.query(Candle).filter(Candle.market_id == position.market_id)
    timeframe = _source_timeframe(position)
    if timeframe:
        candle = (
            query.filter(Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .first()
        )
        if candle is not None:
            return candle
    return query.order_by(Candle.open_time.desc()).first()


def _serialize_market(market: Market | None) -> dict | None:
    if market is None:
        return None
    return {
        "id": market.id,
        "exchange": market.exchange,
        "symbol": market.symbol,
        "base": market.base,
        "quote": market.quote,
        "market_type": market.market_type,
        "active": market.active,
        "metadata_json": market.metadata_json,
    }


def _serialize_order(row: Order, market: Market | None = None) -> dict:
    return {
        "id": row.id,
        "position_id": row.position_id,
        "signal_id": row.signal_id,
        "market": _serialize_market(market),
        "mode": row.mode,
        "order_type": row.order_type,
        "side": row.side,
        "price": str(row.price) if row.price is not None else None,
        "size": str(row.size),
        "status": row.status,
        "fee": str(row.fee) if row.fee is not None else None,
        "created_at": row.created_at,
    }


def _serialize_position(
    db: Session,
    row: Position,
    orders: list[Order] | None = None,
    *,
    now_utc: datetime | None = None,
) -> dict:
    def serialize_decimal(value: object | None) -> str | None:
        return str(value) if value is not None else None

    now_utc = now_utc or datetime.now(timezone.utc)
    unrealized_pnl = row.unrealized_pnl
    # Single source of truth: trust the worker-updated unrealized_pnl unless
    # it is missing or stale. Falling back to a candle-based recompute keeps
    # the UI consistent while we wait for the next worker tick.
    is_stale = row.pnl_updated_at is None or (now_utc - row.pnl_updated_at) > PNL_STALENESS
    if row.status == "open" and row.mode == "live":
        # For live positions the authoritative P&L comes from Hyperliquid
        # (written by reconcile_live_positions). Never override it with a
        # candle-based estimate — stale candles give wrong sign and magnitude.
        if unrealized_pnl is None:
            mark_str = (row.metadata_json or {}).get("last_mark_price")
            if mark_str:
                try:
                    unrealized_pnl = _calculate_unrealized_pnl(row, Decimal(str(mark_str)))
                except Exception:
                    pass
    elif row.status == "open" and (unrealized_pnl is None or is_stale):
        latest_candle = _latest_candle_for_position(db, row)
        unrealized_pnl = _calculate_unrealized_pnl(row, latest_candle.close if latest_candle else None)
    metrics = build_trade_metrics(row, orders or [], unrealized_pnl=unrealized_pnl)
    return {
        "id": row.id,
        "market_id": row.market_id,
        "market": _serialize_market(row.market),
        "mode": row.mode,
        "side": row.side,
        "entry_price": str(row.entry_price),
        "size": str(row.size),
        "leverage": str(row.leverage) if row.leverage is not None else None,
        "stop_loss": str(row.stop_loss) if row.stop_loss is not None else None,
        "take_profit": str(row.take_profit) if row.take_profit is not None else None,
        "status": row.status,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
        "unrealized_pnl": str(unrealized_pnl) if unrealized_pnl is not None else None,
        "cum_funding_since_open": str(row.cum_funding_since_open)
        if row.cum_funding_since_open is not None
        else None,
        "initial_size": serialize_decimal(metrics.initial_size),
        "notional": serialize_decimal(metrics.notional),
        "initial_risk": serialize_decimal(metrics.initial_risk),
        "total_pnl": serialize_decimal(metrics.total_pnl),
        "return_pct": serialize_decimal(metrics.return_pct),
        "realized_return_pct": serialize_decimal(metrics.realized_return_pct),
        "r_multiple": serialize_decimal(metrics.r_multiple),
        "realized_r_multiple": serialize_decimal(metrics.realized_r_multiple),
        "metadata_json": row.metadata_json,
    }


def _computed_indicator_snapshots(db: Session, market: Market, timeframe: str, limit: int = 250) -> list[dict]:
    rows = (
        db.query(Candle)
        .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return []
    candles = [
        CandleInput(
            open_time=row.open_time,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in reversed(rows)
    ]
    registry = strategy_registry()
    configs = (
        db.query(StrategyConfig)
        .filter(StrategyConfig.timeframe == timeframe, StrategyConfig.enabled.is_(True))
        .order_by(StrategyConfig.strategy_name)
        .all()
    )
    context = StrategyContext(
        market="hyperliquid",
        symbol=market.symbol,
        timeframe=timeframe,
        market_type=market.market_type,
        candles=candles,
    )
    snapshots = []
    for config in configs:
        strategy = registry.get(config.strategy_name)
        if strategy is None:
            continue
        result = strategy.evaluate(context, config.params)
        snapshots.append(
            {
                "indicator_name": result.strategy_name,
                "timeframe": timeframe,
                "timestamp": rows[0].open_time,
                "values": {
                    **result.indicator_values,
                    "_direction": result.direction.value,
                    "_confidence": result.confidence,
                    "_reason": result.reason,
                },
            }
        )
    return snapshots


@router.get("/admin/bot-funnel")
def admin_bot_funnel(db: Session = Depends(get_db)) -> dict:
    """Admin view of the onboarding funnel: counts, conversion, distributions
    and the most recent users. Read-only; no auth here because the API key
    middleware only protects mutating methods (same convention as the rest of
    the dashboard endpoints)."""

    rows = db.query(BotUser).all()
    total = len(rows)
    by_stage: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_language: dict[str, int] = {}
    by_capital: dict[str, int] = {}
    completed = 0
    declined = 0
    paused = 0
    now = datetime.now(timezone.utc)
    completed_last_24h = 0
    completed_last_7d = 0
    for row in rows:
        by_stage[row.onboarding_stage] = by_stage.get(row.onboarding_stage, 0) + 1
        by_source[row.source or "—"] = by_source.get(row.source or "—", 0) + 1
        by_language[row.language] = by_language.get(row.language, 0) + 1
        if row.trade_capital:
            by_capital[row.trade_capital] = by_capital.get(row.trade_capital, 0) + 1
        else:
            by_capital["skipped"] = by_capital.get("skipped", 0) + 1
        if row.onboarding_stage == "completed":
            completed += 1
            if row.risk_ack_at is not None:
                age = now - row.risk_ack_at
                if age <= timedelta(hours=24):
                    completed_last_24h += 1
                if age <= timedelta(days=7):
                    completed_last_7d += 1
        elif row.onboarding_stage == "declined":
            declined += 1
        if row.paused:
            paused += 1

    completion_rate = (completed / total) if total > 0 else 0.0

    # Per-source funnel: count started vs completed for each source so you can
    # tell which channels (reddit, twitter, landing, ...) bring users that
    # actually finish the disclaimer.
    source_funnel: dict[str, dict[str, int]] = {}
    for row in rows:
        src = row.source or "—"
        bucket = source_funnel.setdefault(src, {"started": 0, "completed": 0})
        bucket["started"] += 1
        if row.onboarding_stage == "completed":
            bucket["completed"] += 1

    recent = (
        db.query(BotUser)
        .order_by(BotUser.created_at.desc())
        .limit(20)
        .all()
    )
    recent_payload = [
        {
            "telegram_username": u.telegram_username,
            "first_name": u.first_name,
            "language": u.language,
            "trade_capital": u.trade_capital,
            "source": u.source,
            "onboarding_stage": u.onboarding_stage,
            "paused": u.paused,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "risk_ack_at": u.risk_ack_at.isoformat() if u.risk_ack_at else None,
            "joined_channel_at": u.joined_channel_at.isoformat() if u.joined_channel_at else None,
        }
        for u in recent
    ]

    return {
        "total": total,
        "completed": completed,
        "declined": declined,
        "paused": paused,
        "completion_rate": round(completion_rate, 4),
        "completed_last_24h": completed_last_24h,
        "completed_last_7d": completed_last_7d,
        "by_stage": by_stage,
        "by_source": by_source,
        "by_language": by_language,
        "by_capital": by_capital,
        "source_funnel": source_funnel,
        "recent": recent_payload,
    }


@router.get("/public/bot-info")
def public_bot_info() -> dict:
    """Public, unauthenticated endpoint used by the landing page to render
    the "Join the channel" CTA without leaking any credentials. Returns the
    bot username (so the frontend can build a https://t.me/<username>?start=...
    deep link) and a hint about whether the funnel is configured at all."""
    settings = get_settings()
    return {
        "bot_username": settings.landing_bot_username,
        "configured": bool(settings.landing_bot_username and settings.public_bot_token),
    }


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    mode = effective_operation_mode(db, settings)
    enable_live_trading = effective_live_trading_enabled(db, settings)
    live_settings = effective_live_execution_settings(db, settings)
    checks = []
    if not settings.hyperliquid_account_address:
        checks.append("HYPERLIQUID_ACCOUNT_ADDRESS missing")
    if not settings.hyperliquid_private_key:
        checks.append("HYPERLIQUID_PRIVATE_KEY missing")
    if settings.hyperliquid_network == "mainnet" and not live_settings["enable_mainnet_live_trading"]:
        checks.append("mainnet live trading not enabled")
    return {
        "status": "ok",
        "mode": mode.value,
        "network": settings.hyperliquid_network,
        "enable_live_trading": enable_live_trading,
        "live_trading_allowed": mode.value == "AUTO_TRADE" and enable_live_trading and not checks,
        "live_readiness": {
            "account_address_configured": bool(settings.hyperliquid_account_address),
            "private_key_configured": bool(settings.hyperliquid_private_key),
            "mainnet_live_enabled": bool(live_settings["enable_mainnet_live_trading"]),
            "checks": checks,
        },
        "worker": _worker_status(db),
    }


# Worker tick cadence is 60s (worker/main.py). Beyond ~3x interval we treat the
# worker as unhealthy — gives one missed cycle of grace before alarming.
WORKER_HEALTHY_THRESHOLD_SECONDS = 180
SYSTEM_SERVICES = {
    "backend": {
        "label": "com.trado.backend",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.backend.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-backend.err.log",
    },
    "worker": {
        "label": "com.trado.worker",
        "plist": FilePath.home() / "Library/LaunchAgents/com.trado.worker.plist",
        "error_log": FilePath.home() / "Library/Logs/trado-worker.err.log",
    },
}
SYSTEM_ACTIONS = {"start", "stop", "reboot"}


def _worker_status(db: Session) -> dict:
    # Heartbeat is written at the start of each tick (independent of cycle
    # duration), so a long first tick after restart still surfaces as alive.
    heartbeat = db.get(AppSetting, "worker_heartbeat")
    heartbeat_value = heartbeat.value if heartbeat and isinstance(heartbeat.value, dict) else {}
    heartbeat_ts = heartbeat_value.get("timestamp") if isinstance(heartbeat_value, dict) else None
    last_heartbeat_at: datetime | None = None
    if isinstance(heartbeat_ts, str):
        try:
            last_heartbeat_at = datetime.fromisoformat(heartbeat_ts)
        except ValueError:
            last_heartbeat_at = None
    last_cycle = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "worker_cycle")
        .order_by(DecisionLog.timestamp.desc())
        .first()
    )
    last_error = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "worker_cycle_failed")
        .order_by(DecisionLog.timestamp.desc())
        .first()
    )
    now = datetime.now(timezone.utc)
    last_run_at = last_heartbeat_at or (last_cycle.timestamp if last_cycle else None)
    seconds_since = (now - last_run_at).total_seconds() if last_run_at else None
    return {
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "seconds_since_last_run": seconds_since,
        "healthy": seconds_since is not None and seconds_since < WORKER_HEALTHY_THRESHOLD_SECONDS,
        "phase": heartbeat_value.get("phase") if isinstance(heartbeat_value, dict) else None,
        "last_cycle_at": last_cycle.timestamp.isoformat() if last_cycle else None,
        "last_cycle": dict(last_cycle.data) if last_cycle and last_cycle.data else None,
        "last_error_at": last_error.timestamp.isoformat() if last_error else None,
        "last_error_message": (last_error.data or {}).get("error") if last_error else None,
    }


def _service_script(service: str, action: str) -> str:
    config = SYSTEM_SERVICES[service]
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{config['label']}"
    plist = FilePath(str(config["plist"]))
    if action in {"start", "reboot"} and not plist.exists():
        raise HTTPException(status_code=404, detail=f"LaunchAgent plist not found: {plist}")

    quoted_domain = shlex.quote(domain)
    quoted_target = shlex.quote(target)
    quoted_plist = shlex.quote(str(plist))

    if action == "stop":
        command = f"/bin/launchctl bootout {quoted_target}"
    elif action == "start":
        command = (
            f"/bin/launchctl bootstrap {quoted_domain} {quoted_plist}; "
            f"/bin/launchctl kickstart {quoted_target}"
        )
    else:
        command = (
            f"/bin/launchctl kickstart -k {quoted_target} || "
            f"/bin/launchctl bootstrap {quoted_domain} {quoted_plist}"
        )
    return f"sleep 0.2; {command}"


def _schedule_service_action(service: str, action: str) -> None:
    script = _service_script(service, action)
    subprocess.Popen(
        ["/bin/sh", "-lc", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _tail_error_log(path: FilePath, limit: int) -> dict:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "updated_at": None,
            "lines": [],
        }
    try:
        with path.open("rb") as handle:
            lines = deque(handle, maxlen=limit)
    except OSError as exc:
        error_id = _log_and_redact("system_error_log_read_failed", exc)
        raise HTTPException(status_code=500, detail=f"Failed to read log, error_id={error_id}") from exc

    return {
        "path": str(path),
        "exists": True,
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "lines": [line.decode("utf-8", errors="replace").rstrip("\n") for line in lines],
    }


@router.get("/market-intelligence")
def market_intelligence() -> dict:
    client = market_data_client()
    fear_greed = client.latest_fear_greed()
    altcoin_season = client.latest_altcoin_season()
    global_market = client.latest_global_market()
    return {
        "fear_greed": {
            "value": fear_greed.value,
            "classification": fear_greed.classification,
            "timestamp": fear_greed.timestamp,
            "source": fear_greed.source,
        }
        if fear_greed
        else None,
        "altcoin_season": {
            "altcoin_index": altcoin_season.altcoin_index,
            "altcoin_marketcap": altcoin_season.altcoin_marketcap,
            "snapshot_time": altcoin_season.snapshot_time,
            "yearly_high": altcoin_season.yearly_high,
            "yearly_high_date": altcoin_season.yearly_high_date,
            "yearly_low": altcoin_season.yearly_low,
            "yearly_low_date": altcoin_season.yearly_low_date,
            "source": altcoin_season.source,
        }
        if altcoin_season
        else None,
        "global_metrics": {
            "btc_price_usd": global_market.btc_price_usd,
            "btc_price_change_24h_pct": global_market.btc_price_change_24h_pct,
            "total_market_cap_usd": global_market.total_market_cap_usd,
            "total_market_cap_ex_top10_usd": global_market.total_market_cap_ex_top10_usd,
            "total_volume_24h_usd": global_market.total_volume_24h_usd,
            "volume_trend": global_market.volume_trend,
            "btc_dominance_pct": global_market.btc_dominance_pct,
            "market_cap_change_24h_pct": global_market.market_cap_change_24h_pct,
            "last_updated": global_market.last_updated,
            "source": global_market.source,
        }
        if global_market
        else None,
    }


@router.get("/market-intelligence/dashboard")
def market_dashboard_snapshot() -> dict:
    return market_dashboard_service().snapshot()


@router.get("/market-intelligence/coinmarketcap-usage")
def coinmarketcap_usage() -> dict:
    return asdict(market_data_client().key_usage())


@router.get("/market-intelligence/coingecko-usage")
def coingecko_usage() -> dict:
    return asdict(market_data_client().coingecko_key_usage())


@router.get("/markets", response_model=list[MarketOut])
def markets(
    market_type: str | None = None,
    active: bool | None = None,
    has_candles: bool | None = None,
    timeframe: str | None = None,
    db: Session = Depends(get_db),
) -> list[Market]:
    from sqlalchemy import exists as sa_exists
    query = db.query(Market).order_by(Market.market_type, Market.symbol)
    if market_type:
        query = query.filter(Market.market_type == market_type)
    if active is not None:
        query = query.filter(Market.active.is_(active))
    if has_candles:
        candle_filter = sa_exists().where(Candle.market_id == Market.id)
        if timeframe:
            candle_filter = candle_filter.where(Candle.timeframe == timeframe)
        query = query.filter(candle_filter)
    return query.all()


@router.post("/markets/sync")
async def sync_markets(db: Session = Depends(get_db)) -> dict:
    count = await MarketSyncService().sync(db)
    return {"synced": count}


@router.get("/candles", response_model=list[CandleOut])
async def candles(
    symbol: str = Query(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-/]+$"),
    timeframe: str = Query(..., pattern=r"^(5m|15m|30m|1h|4h|1d)$"),
    market_type: str | None = Query(None, max_length=16, pattern=r"^[a-z]+$"),
    limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[Any]:
    market_query = db.query(Market).filter(Market.symbol == symbol)
    if market_type:
        market_query = market_query.filter(Market.market_type == market_type)
    market = market_query.first()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    interval_ms = TIMEFRAME_MS.get(timeframe)
    if interval_ms is not None:
        try:
            now = datetime.now(timezone.utc)
            end_ms = int(now.timestamp() * 1000)
            start_ms = end_ms - interval_ms * limit
            live_rows = await HyperliquidClient(get_settings()).candles(
                coin=market.metadata_json.get("hl_coin", market.symbol),
                interval=timeframe,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            return [parse_hl_candle(row) for row in live_rows][-limit:]
        except Exception:
            pass

    db_rows = (
        db.query(Candle)
        .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(limit)
        .all()
    )[::-1]
    return db_rows


@router.get("/indicators", response_model=list[IndicatorOut])
def indicators(
    symbol: str = Query(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._\-/]+$"),
    timeframe: str = Query(..., pattern=r"^(5m|15m|30m|1h|4h|1d)$"),
    market_type: str | None = Query(None, max_length=16, pattern=r"^[a-z]+$"),
    db: Session = Depends(get_db),
) -> list[IndicatorOut] | list[dict]:
    market_query = db.query(Market).filter(Market.symbol == symbol)
    if market_type:
        market_query = market_query.filter(Market.market_type == market_type)
    market = market_query.first()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    latest = (
        db.query(Indicator.timestamp)
        .filter(Indicator.market_id == market.id, Indicator.timeframe == timeframe)
        .order_by(Indicator.timestamp.desc())
        .first()
    )
    if latest is None:
        return _computed_indicator_snapshots(db, market, timeframe)
    row = (
        db.query(Indicator)
        .filter(
            Indicator.market_id == market.id,
            Indicator.timeframe == timeframe,
            Indicator.timestamp == latest[0],
        )
        .first()
    )
    if row is None:
        return []
    return [
        IndicatorOut(
            indicator_name=name,
            timeframe=row.timeframe,
            timestamp=row.timestamp,
            values=vals,
        )
        for name, vals in sorted(row.values.items())
    ]


def _enrich_signal_with_calibration(signal: Signal, db: Session) -> SignalOut:
    """Costruisce SignalOut con i campi di calibrazione popolati via lookup nella
    cached calibration table. Tolerante: in caso di errore (es. DB transitorio)
    ritorna il segnale senza calibrazione invece di sollevare."""
    payload = SignalOut.model_validate(signal)
    try:
        table = cached_calibration_table(db)
        metrics = table.lookup(
            direction=signal.direction,
            timeframe=signal.timeframe,
            score=signal.score,
        )
    except Exception:  # pragma: no cover — tolerante a transient DB errors
        return payload
    if metrics is None:
        return payload
    return payload.model_copy(
        update={
            "calibrated_expected_r": metrics.expected_r,
            "calibrated_win_rate": metrics.shrunk_win_rate,
            "calibration_n_samples": metrics.n_samples,
            "calibration_confidence": metrics.confidence,
        }
    )


@router.get("/signals", response_model=list[SignalOut])
def signals(
    status: str | None = None,
    min_score: int | None = None,
    direction: str | None = None,
    timeframe: str | None = None,
    db: Session = Depends(get_db),
) -> list[SignalOut]:
    query = db.query(Signal).options(joinedload(Signal.market)).order_by(Signal.created_at.desc())
    if status:
        query = query.filter(Signal.status == status)
    if min_score is not None:
        query = query.filter(Signal.score >= min_score)
    if direction:
        query = query.filter(Signal.direction == direction)
    if timeframe:
        query = query.filter(Signal.timeframe == timeframe)
    rows = query.limit(250).all()
    return [_enrich_signal_with_calibration(signal, db) for signal in rows]


@router.get("/signals/{signal_id}", response_model=SignalOut)
def signal_detail(signal_id: int, db: Session = Depends(get_db)) -> SignalOut:
    signal = db.query(Signal).options(joinedload(Signal.market)).filter(Signal.id == signal_id).first()
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return _enrich_signal_with_calibration(signal, db)


@router.post("/signals/{signal_id}/ignore")
def ignore_signal(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.status == "executed":
        raise HTTPException(status_code=409, detail="Executed signals cannot be ignored")
    signal.status = "ignored"
    db.commit()
    return {"status": "ignored"}


@router.post("/signals/{signal_id}/reopen")
def reopen_signal(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.status != "ignored":
        raise HTTPException(status_code=409, detail=f"Signal is {signal.status}, not ignored")
    signal.status = "new"
    db.add(
        DecisionLog(
            event_type="signal_reopened",
            market_id=signal.market_id,
            signal_id=signal.id,
            message="Signal reopened from dashboard",
            data={},
        )
    )
    db.commit()
    return {"status": "new"}


@router.delete("/signals/{signal_id}")
def delete_signal(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    market_id = signal.market_id
    db.query(Order).filter(Order.signal_id == signal_id).update({Order.signal_id: None}, synchronize_session=False)
    db.query(DecisionLog).filter(DecisionLog.signal_id == signal_id).update(
        {DecisionLog.signal_id: None}, synchronize_session=False
    )
    db.query(Alert).filter(Alert.signal_id == signal_id).update({Alert.signal_id: None}, synchronize_session=False)
    db.add(
        DecisionLog(
            event_type="signal_deleted",
            market_id=market_id,
            message="Signal deleted from dashboard",
            data={"deleted_signal_id": signal_id},
        )
    )
    db.delete(signal)
    db.commit()
    return {"status": "deleted"}


@router.post("/signals/{signal_id}/execute-paper")
def execute_paper(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.status != "new":
        raise HTTPException(status_code=409, detail=f"Signal is already {signal.status}")
    settings = get_settings()
    risk_settings = db.get(AppSetting, "risk")
    min_risk_reward = max(float(risk_settings.value.get("min_risk_reward", settings.min_risk_reward)) if risk_settings else settings.min_risk_reward, 2.0)
    if signal.risk_reward is not None and float(signal.risk_reward) < min_risk_reward:
        raise HTTPException(status_code=422, detail=f"Signal R/R {signal.risk_reward} is below minimum {min_risk_reward}")
    position = OrderExecutionEngine(settings).execute_paper(db, signal)
    if position is None:
        raise HTTPException(status_code=500, detail="Failed to execute paper position")
    return {"position_id": position.id, "status": "opened"}


@router.post("/signals/{signal_id}/execute-live")
def execute_live(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.status != "new":
        raise HTTPException(status_code=409, detail=f"Signal is already {signal.status}")
    try:
        position = OrderExecutionEngine(get_settings()).execute_live(db, signal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"position_id": position.id, "status": "submitted"}


@router.get("/positions")
def positions(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Position)
        .options(joinedload(Position.market))
        .order_by(Position.opened_at.desc())
        .limit(POSITION_HISTORY_LIMIT)
        .all()
    )
    position_ids = [row.id for row in rows]
    orders_by_position: dict[int, list[Order]] = {position_id: [] for position_id in position_ids}
    if position_ids:
        position_orders = (
            db.query(Order)
            .filter(Order.position_id.in_(position_ids))
            .order_by(Order.created_at.asc())
            .all()
        )
        for order in position_orders:
            if order.position_id is not None:
                orders_by_position.setdefault(order.position_id, []).append(order)

    now_utc = datetime.now(timezone.utc)
    return [
        _serialize_position(
            db,
            row,
            orders_by_position.get(row.id, []),
            now_utc=now_utc,
        )
        for row in rows
    ]


@router.get("/positions/history")
def position_history(
    period: str = Query("7d"),
    mode: str = Query("all"),
    market_type: str = Query("all"),
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict:
    valid_periods = {"1d", "7d", "30d", "all"}
    valid_modes = {"all", "paper", "live"}
    valid_market_types = {"all", "perp", "spot"}
    if period not in valid_periods:
        raise HTTPException(status_code=422, detail="Invalid period")
    if mode not in valid_modes:
        raise HTTPException(status_code=422, detail="Invalid mode")
    if market_type not in valid_market_types:
        raise HTTPException(status_code=422, detail="Invalid market_type")

    query = (
        db.query(Position)
        .join(Position.market)
        .options(joinedload(Position.market))
        .filter(or_(Position.status != "open", Position.realized_pnl.is_not(None)))
    )
    if period != "all":
        cutoff = datetime.now(timezone.utc) - {
            "1d": timedelta(days=1),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }[period]
        query = query.filter(
            or_(
                Position.closed_at >= cutoff,
                Position.closed_at.is_(None) & (Position.opened_at >= cutoff),
            )
        )
    if mode != "all":
        query = query.filter(Position.mode == mode)
    if market_type != "all":
        query = query.filter(Market.market_type == market_type)

    total = query.count()
    rows = (
        query.order_by(Position.closed_at.desc().nullslast(), Position.opened_at.desc())
        .limit(limit)
        .all()
    )
    position_ids = [row.id for row in rows]
    orders_by_position: dict[int, list[Order]] = {position_id: [] for position_id in position_ids}
    if position_ids:
        history_orders = (
            db.query(Order)
            .filter(
                Order.position_id.in_(position_ids),
                Order.order_type.in_(HISTORY_ORDER_TYPES),
                or_(Order.status == "filled", Order.fee.is_not(None)),
            )
            .order_by(Order.created_at.asc())
            .all()
        )
        for order in history_orders:
            if order.position_id is not None:
                orders_by_position.setdefault(order.position_id, []).append(order)

    now_utc = datetime.now(timezone.utc)
    positions_payload = [
        _serialize_position(
            db,
            row,
            orders_by_position.get(row.id, []),
            now_utc=now_utc,
        )
        for row in rows
    ]
    markets_by_position_id = {row.id: row.market for row in rows}
    orders_payload = [
        _serialize_order(order, markets_by_position_id.get(order.position_id or -1))
        for order in sorted(
            [order for orders in orders_by_position.values() for order in orders],
            key=lambda row: row.created_at,
            reverse=True,
        )
    ]
    return {
        "positions": positions_payload,
        "orders": orders_payload,
        "total": total,
        "filters": {
            "period": period,
            "mode": mode,
            "market_type": market_type,
            "limit": limit,
            "returned": len(rows),
        },
    }


@router.post("/positions/{position_id}/close")
def close_position(position_id: int, db: Session = Depends(get_db)) -> dict:
    position = db.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "open":
        return {"status": position.status}
    executor = OrderExecutionEngine(get_settings())
    if position.mode == "live":
        executor.close_live_position(db, position, "manual")
    else:
        executor.close_position(db, position, position.entry_price, "manual")
    return {"status": "closed"}


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)) -> dict:
    position = db.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    market_id = position.market_id
    status = position.status
    db.query(Order).filter(Order.position_id == position_id).update(
        {Order.position_id: None}, synchronize_session=False
    )
    db.query(DecisionLog).filter(DecisionLog.position_id == position_id).update(
        {DecisionLog.position_id: None}, synchronize_session=False
    )
    db.add(
        DecisionLog(
            event_type="position_deleted",
            market_id=market_id,
            message="Position deleted from dashboard",
            data={"deleted_position_id": position_id, "status": status},
        )
    )
    db.delete(position)
    db.commit()
    return {"status": "deleted"}


@router.get("/orders")
def orders(db: Session = Depends(get_db)) -> list[dict]:
    recent_rows = db.query(Order).order_by(Order.created_at.desc()).limit(ORDER_HISTORY_LIMIT).all()
    rows_by_id = {row.id: row for row in recent_rows}

    position_ids = [
        row_id
        for (row_id,) in db.query(Position.id)
        .order_by(Position.opened_at.desc())
        .limit(POSITION_HISTORY_LIMIT)
        .all()
    ]
    if position_ids:
        context_rows = (
            db.query(Order)
            .filter(
                Order.position_id.in_(position_ids),
                Order.order_type.in_(EXIT_ORDER_TYPES),
                Order.status == "filled",
            )
            .all()
        )
        rows_by_id.update({row.id: row for row in context_rows})

    rows = sorted(rows_by_id.values(), key=lambda row: row.created_at, reverse=True)
    signal_ids = {row.signal_id for row in rows if row.signal_id is not None}
    order_position_ids = {row.position_id for row in rows if row.position_id is not None}
    signals_by_id = (
        {
            row.id: row
            for row in db.query(Signal)
            .options(joinedload(Signal.market))
            .filter(Signal.id.in_(signal_ids))
            .all()
        }
        if signal_ids
        else {}
    )
    positions_by_id = (
        {
            row.id: row
            for row in db.query(Position)
            .options(joinedload(Position.market))
            .filter(Position.id.in_(order_position_ids))
            .all()
        }
        if order_position_ids
        else {}
    )

    result = []
    for row in rows:
        position = positions_by_id.get(row.position_id) if row.position_id is not None else None
        signal = signals_by_id.get(row.signal_id) if row.signal_id is not None else None
        result.append(_serialize_order(row, position.market if position else signal.market if signal else None))
    return result


@router.get("/analytics/signals")
def signal_analytics(
    timeframe: str | None = None,
    market_id: int | None = None,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, timeframe=timeframe, market_id=market_id, limit=limit)
    signal_ids = {
        int(position.metadata_json["source_signal_id"])
        for position in positions
        if isinstance(position.metadata_json, dict)
        and position.metadata_json.get("source_signal_id") is not None
    }
    signals_by_id = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    by_timeframe: dict[str, list[Position]] = {}
    by_direction: dict[str, list[Position]] = {}
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_timeframe = metadata.get("source_timeframe")
        if isinstance(source_timeframe, str):
            by_timeframe.setdefault(source_timeframe, []).append(position)
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is not None:
            try:
                signal = signals_by_id.get(int(source_signal_id))
            except (TypeError, ValueError):
                signal = None
        else:
            signal = None
        if signal is not None:
            by_direction.setdefault(signal.direction, []).append(position)

    return {
        "filters": {"timeframe": timeframe, "market_id": market_id, "limit": limit},
        "overall": serialize_stats(analyzer.summarize_positions(positions)),
        "by_strategy": {
            strategy_name: serialize_stats(stats)
            for strategy_name, stats in analyzer.strategy_stats(db, timeframe, market_id, limit).items()
        },
        "by_timeframe": {
            key: serialize_stats(analyzer.summarize_positions(value))
            for key, value in sorted(by_timeframe.items())
        },
        "by_direction": {
            key: serialize_stats(analyzer.summarize_positions(value))
            for key, value in sorted(by_direction.items())
        },
    }


@router.get("/analytics/diagnostics")
def analytics_diagnostics(
    lookback: int = Query(200, ge=10, le=2000),
    history_limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    return build_diagnostics_report(db, lookback=lookback, history_limit=history_limit)


@router.get("/analytics/equity-slope")
def analytics_equity_slope(
    lookback: int = Query(500, ge=50, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    return build_equity_slope_diagnostics(db, lookback=lookback)


@router.get("/analytics/outcome-profile")
def analytics_outcome_profile(
    lookback: int = Query(500, ge=50, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    return build_outcome_profile(db, lookback=lookback)


@router.get("/analytics/live-trade-costs")
def analytics_live_trade_costs(
    lookback: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    """Scompone il PnL live (lordo da closedPnl) in fee, funding e slippage per
    esporre il PnL netto reale dei trade live chiusi."""
    report = aggregate_live_costs(
        db, taker_fee_rate=get_settings().live_taker_fee_rate, limit=lookback
    )
    return serialize_live_cost_report(report)


@router.get("/analytics/expected-r-calibration")
def analytics_expected_r_calibration(
    lookback: int = Query(CALIBRATION_DEFAULT_LOOKBACK, ge=50, le=2000),
    enforce_monotonic: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict:
    """Calibrazione empirica score → Expected R-multiple per (direction, timeframe, score_bucket).

    Distinto dall'endpoint legacy `/analytics/score-calibration` (binning PnL grezzo
    senza shrinkage). Questo applica Bayesian shrinkage e usa R-multiple normalizzato
    per rischio iniziale invece di PnL assoluto.
    """
    table = build_calibration_table_from_db(
        db,
        lookback=lookback,
        enforce_monotonic=enforce_monotonic,
    )
    return serialize_table(table)


@router.get("/analytics/score-calibration")
def analytics_score_calibration(
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    analyzer = SignalPerformanceAnalyzer()
    positions = analyzer.closed_paper_positions(db, limit=limit)

    signal_ids = {
        int(position.metadata_json["source_signal_id"])
        for position in positions
        if isinstance(position.metadata_json, dict)
        and position.metadata_json.get("source_signal_id") is not None
    }
    signals_by_id = (
        {signal.id: signal for signal in db.query(Signal).filter(Signal.id.in_(signal_ids)).all()}
        if signal_ids
        else {}
    )

    pairs: list[tuple[int, float, Any]] = []
    for position in positions:
        metadata = position.metadata_json if isinstance(position.metadata_json, dict) else {}
        source_signal_id = metadata.get("source_signal_id")
        if source_signal_id is None:
            continue
        try:
            signal = signals_by_id.get(int(source_signal_id))
        except (TypeError, ValueError):
            continue
        if signal is None or signal.score is None:
            continue
        pnl = float(position.realized_pnl or 0)
        closed_at = position.closed_at or position.opened_at
        pairs.append((signal.score, pnl, closed_at))

    if not pairs:
        return {"buckets": [], "correlation": None, "total_trades": 0, "by_week": []}

    bucket_data: dict[int, list[float]] = {}
    for score, pnl, _ in pairs:
        bucket = (score // 10) * 10
        bucket_data.setdefault(bucket, []).append(pnl)

    buckets = []
    for min_score in sorted(bucket_data.keys()):
        pnls = bucket_data[min_score]
        wins = sum(1 for p in pnls if p > 0)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        buckets.append({
            "range": f"{min_score}-{min_score + 9}",
            "min_score": min_score,
            "count": len(pnls),
            "win_rate": wins / len(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0),
        })

    scores_list = [p[0] for p in pairs]
    pnls_list = [p[1] for p in pairs]
    n = len(scores_list)
    correlation: float | None = None
    if n >= 2:
        mean_s = sum(scores_list) / n
        mean_p = sum(pnls_list) / n
        cov = sum((s - mean_s) * (p - mean_p) for s, p in zip(scores_list, pnls_list)) / n
        std_s = (sum((s - mean_s) ** 2 for s in scores_list) / n) ** 0.5
        std_p = (sum((p - mean_p) ** 2 for p in pnls_list) / n) ** 0.5
        if std_s > 0 and std_p > 0:
            correlation = round(cov / (std_s * std_p), 3)

    now = datetime.now(timezone.utc)
    period_days = 14
    n_periods = 8
    by_week = []
    for i in range(n_periods - 1, -1, -1):
        period_end = now - timedelta(days=i * period_days)
        period_start = period_end - timedelta(days=period_days)
        period_pairs = []
        for s, p, dt in pairs:
            if dt is None:
                continue
            dt_utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            if period_start <= dt_utc < period_end:
                period_pairs.append((s, p))
        if not period_pairs:
            continue
        wins = sum(1 for _, p in period_pairs if p > 0)
        by_week.append({
            "period": period_start.strftime("%Y-%m-%d"),
            "avg_score": round(sum(s for s, _ in period_pairs) / len(period_pairs), 1),
            "win_rate": round(wins / len(period_pairs), 3),
            "count": len(period_pairs),
        })

    return {
        "buckets": buckets,
        "correlation": correlation,
        "total_trades": len(pairs),
        "by_week": by_week,
    }


@router.post("/analytics/report/send")
async def send_diagnostic_report(
    lookback: int = Query(200, ge=10, le=2000),
    db: Session = Depends(get_db),
) -> dict:
    from app.notifications.diagnostic_report import DiagnosticReporter
    from app.notifications.telegram import TelegramNotifier
    settings = get_settings()
    notifier = TelegramNotifier(settings)
    return await DiagnosticReporter().send(db, notifier, lookback=lookback)


@router.get("/analytics/calibration")
def calibration_preview(db: Session = Depends(get_db)) -> dict:
    result = AutoCalibrationEngine().preview(db)
    payload = serialize_calibration(result)
    state = db.get(AppSetting, "auto_calibration_state")
    baseline = db.get(AppSetting, "auto_calibration_baseline")
    payload["state"] = state.value if state is not None else {}
    payload["baseline"] = baseline.value if baseline is not None else {}
    db.rollback()
    return payload


@router.post("/auto-calibration/run")
def run_auto_calibration(db: Session = Depends(get_db)) -> dict:
    result = AutoCalibrationEngine().run(db, force=True)
    return serialize_calibration(result)


@router.get("/auto-tuner/status")
def auto_tuner_status(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().status(db)


@router.get("/auto-tuner/source-tests")
def auto_tuner_source_tests(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().source_tests(db)


@router.get("/auto-tuner/strategy-tests")
def auto_tuner_strategy_tests(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().strategy_tests(db)


@router.post("/auto-tuner/source-tests/{source}/start")
def auto_tuner_start_source_test(
    source: str = Path(..., pattern="^(whale_flow|fear_greed)$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().start_source_test(db, source)
    status = result.get("status")
    if status == "unknown_source":
        raise HTTPException(status_code=404, detail=result)
    if status == "conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/auto-tuner/strategy-tests/{timeframe}/{strategy}/start")
def auto_tuner_start_strategy_test(
    timeframe: str = Path(..., pattern="^[0-9a-zA-Z]+$"),
    strategy: str = Path(..., pattern="^[a-zA-Z0-9_-]+$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().start_strategy_test(db, timeframe, strategy)
    status = result.get("status")
    if status == "unsupported_strategy_test":
        raise HTTPException(status_code=404, detail=result)
    if status == "conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/auto-tuner/strategy-tests/{timeframe}/{strategy}/queue")
def auto_tuner_queue_strategy_test(
    timeframe: str = Path(..., pattern="^[0-9a-zA-Z]+$"),
    strategy: str = Path(..., pattern="^[a-zA-Z0-9_-]+$"),
    db: Session = Depends(get_db),
) -> dict:
    result = AutoTuner().queue_strategy_test(db, timeframe, strategy)
    status = result.get("status")
    if status == "unsupported_strategy_test":
        raise HTTPException(status_code=404, detail=result)
    if status == "queue_conflict":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.delete("/auto-tuner/strategy-tests/queue")
def auto_tuner_clear_queued_strategy_test(db: Session = Depends(get_db)) -> dict:
    result = AutoTuner().clear_queued_strategy_test(db)
    if result.get("status") == "queue_empty":
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/auto-tuner/run")
def auto_tuner_run(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().run(db)


@router.post("/auto-tuner/pause")
def auto_tuner_pause(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().pause(db)


@router.post("/auto-tuner/resume")
def auto_tuner_resume(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().resume(db)


@router.post("/auto-tuner/rollback")
def auto_tuner_rollback(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().rollback_current(db)


@router.post("/auto-tuner/reset")
def auto_tuner_reset(db: Session = Depends(get_db)) -> dict:
    return AutoTuner().reset(db)


@router.post("/auto-calibration/baseline/reset")
def reset_calibration_baseline(db: Session = Depends(get_db)) -> dict:
    deleted = (
        db.query(AppSetting)
        .filter(AppSetting.key == "auto_calibration_baseline")
        .delete()
    )
    db.add(
        DecisionLog(
            event_type="auto_calibration_baseline_reset",
            message="Auto calibration baseline reset",
            data={"deleted": int(deleted)},
        )
    )
    db.commit()
    return {"ok": True, "deleted": int(deleted)}


@router.get("/analytics/calibration/history")
def calibration_history(db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    event_types = [
        "auto_calibration_applied",
        "auto_calibration_checked",
        "auto_calibration_skipped",
        "auto_calibration_failed",
    ]
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(event_types))
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "message": row.message,
            "status": (row.data or {}).get("status"),
            "reason": (row.data or {}).get("reason"),
            "overall": (row.data or {}).get("overall"),
            "changes": (row.data or {}).get("changes", []),
            "error": (row.data or {}).get("error"),
            "force": (row.data or {}).get("force"),
        }
        for row in rows
    ]


@router.get("/analytics/calibration/stability")
def calibration_stability(db: Session = Depends(get_db)) -> dict:
    """Diagnostica walk-forward per strategia: train vs test stats + factor history.

    Mostra per ogni `(timeframe:strategy)`:
    - WR / PnL / PF su finestra di train (più vecchia) e test (più recente)
    - "concordance": train e test puntano nello stesso verso (defensive vs baseline)?
    - factor_history: storico dei factor decisi dalle ultime N calibrazioni
    - factor_stddev: deviazione standard dei factor recenti (proxy di overfitting)
    """
    import math as _math

    from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats

    analyzer = SignalPerformanceAnalyzer()
    auto_cal_setting = db.get(AppSetting, "auto_calibration")
    settings = (
        auto_cal_setting.value if auto_cal_setting and isinstance(auto_cal_setting.value, dict)
        else {}
    )
    train_ratio = float(settings.get("walk_forward_train_ratio", 0.7))
    min_per_side = int(settings.get("walk_forward_min_per_side", 5))
    lookback = int(settings.get("lookback_trades", 500))

    state_setting = db.get(AppSetting, "auto_calibration_state")
    state_value = (
        state_setting.value if state_setting and isinstance(state_setting.value, dict)
        else {}
    )
    factor_history = dict(state_value.get("factor_history") or {})

    strategies = db.query(StrategyConfig).all()
    timeframes = sorted({s.timeframe for s in strategies})

    by_strategy: list[dict] = []
    for timeframe in timeframes:
        positions_by_strategy = analyzer.strategy_positions(
            db, timeframe=timeframe, limit=lookback
        )
        for strategy_config in strategies:
            if strategy_config.timeframe != timeframe:
                continue
            key = f"{timeframe}:{strategy_config.strategy_name}"
            positions = positions_by_strategy.get(strategy_config.strategy_name, [])
            history = factor_history.get(key, [])
            stddev = 0.0
            if len(history) >= 2:
                mean = sum(history) / len(history)
                stddev = _math.sqrt(sum((v - mean) ** 2 for v in history) / (len(history) - 1))
            split = SignalPerformanceAnalyzer.holdout_split_stats(
                positions, train_ratio=train_ratio, min_samples_per_side=min_per_side
            )
            entry: dict = {
                "key": key,
                "timeframe": timeframe,
                "strategy": strategy_config.strategy_name,
                "enabled": strategy_config.enabled,
                "weight": strategy_config.weight,
                "trades": len(positions),
                "factor_history": history,
                "factor_stddev": round(stddev, 4),
            }
            if split is None:
                entry["holdout"] = None
            else:
                train, test = split
                entry["holdout"] = {
                    "train": serialize_stats(train),
                    "test": serialize_stats(test),
                    "wr_delta": round(test.win_rate - train.win_rate, 4),
                }
            by_strategy.append(entry)
    return {
        "settings": {
            "walk_forward_train_ratio": train_ratio,
            "walk_forward_min_per_side": min_per_side,
            "factor_history_window": int(settings.get("factor_history_window", 5)),
            "factor_history_std_threshold": float(
                settings.get("factor_history_std_threshold", 0.25)
            ),
        },
        "by_strategy": by_strategy,
    }


@router.get("/analytics/worker-cycles")
def worker_cycles(db: Session = Depends(get_db), limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(["worker_cycle", "worker_cycle_failed"]))
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "data": row.data or {},
        }
        for row in rows
    ]


@router.post("/system/services/{service}/{action}")
def system_service_action(
    service: str = Path(..., pattern=r"^(backend|worker)$"),
    action: str = Path(..., pattern=r"^(start|stop|reboot)$"),
) -> dict:
    if service not in SYSTEM_SERVICES or action not in SYSTEM_ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown service action")
    _schedule_service_action(service, action)
    return {
        "status": "scheduled",
        "service": service,
        "action": action,
        "label": SYSTEM_SERVICES[service]["label"],
    }


@router.get("/system/error-logs")
def system_error_logs(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {
        service: _tail_error_log(FilePath(str(config["error_log"])), limit)
        for service, config in SYSTEM_SERVICES.items()
    }


@router.get("/analytics/signal-rejections")
def signal_rejections(db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)) -> dict:
    rows = (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type == "live_order_rejected")
        .order_by(DecisionLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    reason_counts: dict[str, int] = {}
    recent: list[dict] = []
    for row in rows:
        reasons = (row.data or {}).get("reasons", [])
        if isinstance(reasons, list):
            for r in reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1
        recent.append({
            "id": row.id,
            "timestamp": row.timestamp,
            "reasons": reasons,
        })
    return {"total": len(rows), "by_reason": reason_counts, "recent": recent[:10]}


@router.get("/settings")
def settings(db: Session = Depends(get_db)) -> dict:
    return {row.key: row.value for row in db.query(AppSetting).all()}


@router.patch("/settings")
def patch_settings(payload: dict[str, SettingsPatch], db: Session = Depends(get_db)) -> dict:
    for key, patch in payload.items():
        setting = db.get(AppSetting, key)
        if setting is None:
            setting = AppSetting(key=key, value=patch.value)
            db.add(setting)
        else:
            setting.value = patch.value

        if key == "timeframes":
            enabled = patch.value.get("enabled") if isinstance(patch.value, dict) else None
            if isinstance(enabled, list):
                existing_tfs = {tf for (tf,) in db.query(StrategyConfig.timeframe).distinct()}
                for timeframe in enabled:
                    if timeframe not in existing_tfs:
                        seed_timeframe(db, timeframe)

    db.commit()
    return {"status": "updated"}


@router.get("/strategies")
def strategies(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": row.id,
            "strategy_name": row.strategy_name,
            "enabled": row.enabled,
            "timeframe": row.timeframe,
            "params": row.params,
            "weight": row.weight,
        }
        for row in db.query(StrategyConfig).order_by(StrategyConfig.timeframe, StrategyConfig.strategy_name).all()
    ]


@router.patch("/strategies/{strategy_id}")
def patch_strategy(strategy_id: int, payload: StrategyPatch, db: Session = Depends(get_db)) -> dict:
    strategy = db.get(StrategyConfig, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if payload.enabled is not None:
        strategy.enabled = payload.enabled
    if payload.params is not None:
        strategy.params = payload.params
    if payload.weight is not None:
        strategy.weight = payload.weight
    db.commit()
    return {"status": "updated"}


@router.post("/kill-switch/enable")
def enable_kill_switch(db: Session = Depends(get_db)) -> dict:
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    setting.value = {"enabled": True}
    db.add(setting)
    db.commit()
    return {"enabled": True}


@router.post("/kill-switch/disable")
def disable_kill_switch(db: Session = Depends(get_db)) -> dict:
    setting = db.get(AppSetting, "kill_switch") or AppSetting(key="kill_switch", value={})
    setting.value = {"enabled": False}
    db.add(setting)
    db.commit()
    return {"enabled": False}


def _decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _string_decimal(value: object | None) -> str | None:
    parsed = _decimal(value)
    return str(parsed) if parsed is not None else None


def _uar_setting_decimal(risk: dict, key: str) -> Decimal:
    default = DEFAULT_UAR_RISK_SETTINGS[key]
    return decimal_or_none(risk.get(key, default)) or Decimal(str(default))


def _redact_address(address: str | None) -> str | None:
    if not address:
        return None
    if len(address) <= 12:
        return address
    return f"{address[:6]}...{address[-4:]}"


def _position_payload(position: dict[str, Any], protection: dict[str, Any] | None = None) -> dict:
    size = _decimal(position.get("szi"))
    payload = {
        "coin": position.get("coin"),
        "side": "long" if size is not None and size > 0 else "short" if size is not None and size < 0 else None,
        "size": str(abs(size)) if size is not None else None,
        "entry_price": _string_decimal(position.get("entryPx")),
        "position_value": _string_decimal(position.get("positionValue")),
        "unrealized_pnl": _string_decimal(position.get("unrealizedPnl")),
        "return_on_equity": _string_decimal(position.get("returnOnEquity")),
        "liquidation_price": _string_decimal(position.get("liquidationPx")),
        "margin_used": _string_decimal(position.get("marginUsed")),
        "leverage": position.get("leverage"),
        "cum_funding": position.get("cumFunding"),
    }
    if protection is not None:
        payload["protection"] = protection
    return payload


def _merge_protection_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    if not states:
        return {
            "status": "untracked",
            "needs_intervention": False,
            "repairable": False,
            "expected_orders": [],
            "active_orders": [],
            "missing_orders": [],
            "rejected_orders": [],
            "issues": [],
            "summary": "No Trado live position linked to this Hyperliquid position",
            "local_position_ids": [],
            "stop_update_failed": False,
            "stop_replacement_attempts": 0,
        }
    expected = sorted({item for state in states for item in state.get("expected_orders", [])})
    active = sorted({item for state in states for item in state.get("active_orders", [])})
    missing = sorted({item for state in states for item in state.get("missing_orders", [])})
    rejected = sorted({item for state in states for item in state.get("rejected_orders", [])})
    issues = [issue for state in states for issue in state.get("issues", [])]
    position_ids = [int(state["local_position_id"]) for state in states if state.get("local_position_id") is not None]
    stop_update_failed = any(bool(state.get("stop_update_failed")) for state in states)
    stop_attempts = max((int(state.get("stop_replacement_attempts") or 0) for state in states), default=0)
    needs_intervention = bool(missing or rejected or stop_update_failed)
    summary_parts: list[str] = []
    if missing:
        summary_parts.append("Missing " + ", ".join(missing))
    if rejected:
        summary_parts.append("Rejected " + ", ".join(rejected))
    if stop_update_failed:
        summary_parts.append("stop update failed")
    return {
        "status": "intervention_required" if needs_intervention else "ok",
        "needs_intervention": needs_intervention,
        "repairable": True,
        "expected_orders": expected,
        "active_orders": active,
        "missing_orders": missing,
        "rejected_orders": rejected,
        "issues": issues,
        "summary": " | ".join(summary_parts) if summary_parts else "All expected protection orders are present",
        "local_position_ids": position_ids,
        "stop_update_failed": stop_update_failed,
        "stop_replacement_attempts": stop_attempts,
    }


def _account_position_for_coin(user_state: dict[str, Any], coin: str) -> dict[str, Any] | None:
    for item in user_state.get("assetPositions", []):
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        if isinstance(position, dict) and position.get("coin") == coin:
            return position
    return None


def _spot_balance_payload(balance: dict[str, Any]) -> dict:
    return {
        "coin": balance.get("coin"),
        "total": _string_decimal(balance.get("total")),
        "hold": _string_decimal(balance.get("hold")),
        "entry_ntl": _string_decimal(balance.get("entryNtl")),
    }


def _predicted_funding_payload(row: Any) -> dict | None:
    if not isinstance(row, list) or len(row) < 2:
        return None
    coin = row[0]
    exchanges = row[1]
    if not isinstance(coin, str) or not isinstance(exchanges, list):
        return None
    hyperliquid = None
    compact_exchanges = []
    for item in exchanges:
        if not isinstance(item, list) or len(item) < 2:
            continue
        exchange = item[0]
        data = item[1] if isinstance(item[1], dict) else {}
        payload = {
            "exchange": exchange,
            "funding_rate": _string_decimal(data.get("fundingRate")),
            "next_funding_time": data.get("nextFundingTime"),
            "funding_interval_hours": data.get("fundingIntervalHours"),
        }
        compact_exchanges.append(payload)
        if exchange == "HlPerp":
            hyperliquid = payload
    return {"coin": coin, "hyperliquid": hyperliquid, "exchanges": compact_exchanges}


def _latest_account_snapshot(db: Session) -> dict | None:
    row = db.query(AccountSnapshot).order_by(AccountSnapshot.timestamp.desc()).first()
    if row is None:
        return None
    return {
        "timestamp": row.timestamp,
        "total_equity": str(row.total_equity) if row.total_equity is not None else None,
        "available_balance": str(row.available_balance) if row.available_balance is not None else None,
        "margin_used": str(row.margin_used) if row.margin_used is not None else None,
        "unrealized_pnl": str(row.unrealized_pnl) if row.unrealized_pnl is not None else None,
    }


async def _fetch_market_meta(client: HyperliquidClient) -> tuple[dict | None, dict | None, str | None]:
    try:
        return await client.meta(), await client.mids(), None
    except Exception as exc:
        return None, None, _log_and_redact("account_snapshot_api_error", exc)


async def _fetch_account_state(
    client: HyperliquidClient,
    address: str | None,
    checked_at: datetime,
) -> tuple[dict | None, dict | None, list[dict], list[dict], str | None]:
    if not address:
        return None, None, [], [], None
    try:
        user_state = await client.user_state(address)
        spot_state = await client.spot_user_state(address)
        open_orders = await client.frontend_open_orders(address)
        end_ms = int(checked_at.timestamp() * 1000)
        day_ago_ms = end_ms - (24 * 60 * 60 * 1000)
        user_funding = await client.user_funding(address, day_ago_ms, end_ms)
        return user_state, spot_state, open_orders, user_funding, None
    except Exception as exc:
        return None, None, [], [], _log_and_redact("account_snapshot_account_error", exc)


async def _fetch_predicted_fundings(client: HyperliquidClient) -> tuple[list[dict], str | None]:
    try:
        raw = await client.predicted_fundings()
        return [
            item
            for item in (_predicted_funding_payload(row) for row in raw)
            if item is not None
        ], None
    except Exception as exc:
        return [], _log_and_redact("account_snapshot_predicted_error", exc)


def _hl_period_for_days(days: int) -> str:
    """Map a day-count to the most appropriate HL portfolio period key."""
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    return "allTime"


def _hl_best_hist(period_map: dict, days: int) -> list:
    """Return the accountValueHistory list that best covers the requested window.

    For windows > 7 days we use allTime (full coverage, ~38 pts).
    For ≤ 7 days we prefer week (higher resolution, ~60 pts) and fall back to allTime.
    For ≤ 1 day we prefer day, then week, then allTime.
    """
    for period in (["day", "week", "allTime"] if days <= 1 else (["week", "allTime"] if days <= 7 else ["allTime", "week"])):
        hist = period_map.get(period, {}).get("accountValueHistory", [])
        if hist:
            return hist
    return []


def _compute_equity_points(raw_pairs: list[tuple[float, float]]) -> list[dict]:
    """Given [(ts_ms, equity), ...] sorted ascending, return point dicts with drawdown."""
    points = []
    peak: float | None = None
    for ts_ms, equity in raw_pairs:
        if equity <= 0:
            continue
        if peak is None or equity > peak:
            peak = equity
        dd = ((peak - equity) / peak * 100) if peak and peak > 0 else 0.0
        ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        points.append({"ts": ts_dt.isoformat(), "equity": round(equity, 4), "drawdown_pct": round(dd, 4)})
    return points


@router.get("/account/equity-history")
async def account_equity_history(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    """Return account equity history.

    Primary source: HL portfolio endpoint (authoritative, covers full account value incl. spot).
    Supplementary: local account_snapshots with effective_account_value for recent high-res data.
    """
    settings = get_settings()
    address = settings.hyperliquid_account_address
    since_ms = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000

    # ── Fetch from HL ──────────────────────────────────────────────────────────
    hl_pairs: list[tuple[float, float]] = []
    hl_error: str | None = None
    if address:
        try:
            client = HyperliquidClient(settings)
            portfolio_data = await client.portfolio(address)
            period_map: dict[str, list] = {item[0]: item[1] for item in portfolio_data if isinstance(item, list) and len(item) == 2}
            hist = _hl_best_hist(period_map, days)
            hl_pairs = [
                (float(ts), float(val))
                for ts, val in hist
                if float(ts) >= since_ms and float(val) > 0
            ]
            hl_pairs.sort(key=lambda x: x[0])
        except Exception as exc:
            hl_error = str(exc)

    # ── Fallback: local snapshots (new records with effective_account_value) ──
    local_pairs: list[tuple[float, float]] = []
    if not hl_pairs:
        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
        rows = (
            db.query(AccountSnapshot)
            .filter(
                AccountSnapshot.timestamp >= since_dt,
                AccountSnapshot.effective_account_value.isnot(None),
            )
            .order_by(AccountSnapshot.timestamp.asc())
            .all()
        )
        local_pairs = [
            (row.timestamp.timestamp() * 1000, float(row.effective_account_value))
            for row in rows
            if row.effective_account_value is not None
        ]

    raw_pairs = hl_pairs if hl_pairs else local_pairs
    points = _compute_equity_points(raw_pairs)

    current_equity = points[-1]["equity"] if points else None
    peak_equity = max((p["equity"] for p in points), default=None)
    max_drawdown_pct = max((p["drawdown_pct"] for p in points), default=0.0)
    return {
        "points": points,
        "current_equity": current_equity,
        "peak_equity": peak_equity,
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "days": days,
        "source": "hyperliquid" if hl_pairs else "local",
        "hl_error": hl_error,
    }


@router.get("/account/snapshot")
async def account_snapshot(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    client = HyperliquidClient(settings)
    checked_at = datetime.now(timezone.utc)
    started = perf_counter()
    meta, mids, api_error = await _fetch_market_meta(client)
    api_latency_ms = round((perf_counter() - started) * 1000, 1)

    address = settings.hyperliquid_account_address
    user_state, spot_state, open_orders, user_funding, account_error = await _fetch_account_state(
        client, address, checked_at
    )
    predicted_fundings, predicted_error = await _fetch_predicted_fundings(client)

    margin = (user_state or {}).get("marginSummary", {})
    cross_margin = (user_state or {}).get("crossMarginSummary", {})
    withdrawable = (user_state or {}).get("withdrawable")
    from app.engine.execution import coin_for_market
    from app.services.live_reconciliation import (
        _index_frontend_open_orders,
        protection_state_for_live_position,
    )
    local_live_positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.mode == "live", Position.status == "open")
        .all()
    )
    local_positions_by_coin: dict[str, list[Position]] = {}
    for local_position in local_live_positions:
        if local_position.market is None:
            continue
        try:
            coin = coin_for_market(local_position.market)
        except ValueError:
            continue
        local_positions_by_coin.setdefault(coin, []).append(local_position)
    frontend_open_orders_by_coin = _index_frontend_open_orders(open_orders)
    positions = [
        _position_payload(
            item.get("position", {}),
            protection=_merge_protection_states(
                [
                    protection_state_for_live_position(
                        db,
                        local_position,
                        frontend_open_orders_by_coin.get(str(item.get("position", {}).get("coin") or ""), []),
                    )
                    for local_position in local_positions_by_coin.get(str(item.get("position", {}).get("coin") or ""), [])
                ]
            ),
        )
        for item in (user_state or {}).get("assetPositions", [])
        if isinstance(item, dict)
    ]
    spot_balances = [
        _spot_balance_payload(item)
        for item in (spot_state or {}).get("balances", [])
        if isinstance(item, dict)
    ]
    position_coins = {item.get("coin") for item in positions if item.get("coin")}
    relevant_fundings = [
        item for item in predicted_fundings if item.get("coin") in position_coins
    ] or predicted_fundings[:12]

    total_equity = _decimal(margin.get("accountValue"))
    available_balance = _decimal(withdrawable)
    margin_used = _decimal(margin.get("totalMarginUsed"))
    unrealized_pnl = sum(
        (_decimal(position.get("unrealized_pnl")) or Decimal("0") for position in positions),
        Decimal("0"),
    )
    free_spot_usdc = Decimal("0")
    for bal in spot_balances:
        if bal.get("coin") == "USDC":
            total_usdc = _decimal(bal.get("total")) or Decimal("0")
            hold_usdc = _decimal(bal.get("hold")) or Decimal("0")
            free_spot_usdc = max(Decimal("0"), total_usdc - hold_usdc)
            break
    effective_account_value = (total_equity or Decimal("0")) + free_spot_usdc
    risk_setting = db.get(AppSetting, "risk")
    risk_value = risk_setting.value if risk_setting is not None and isinstance(risk_setting.value, dict) else {}
    uar = calculate_unified_account_ratio(
        user_state,
        free_spot_usdc=free_spot_usdc,
        warn_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_warn_pct"),
        block_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_block_pct"),
        derisk_pct=_uar_setting_decimal(risk_value, "unified_account_ratio_derisk_pct"),
    )
    if user_state is not None:
        db.add(
            AccountSnapshot(
                timestamp=checked_at,
                total_equity=total_equity,
                effective_account_value=effective_account_value,
                free_spot_usdc=free_spot_usdc,
                available_balance=available_balance,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                raw_data={
                    "marginSummary": margin,
                    "crossMarginSummary": cross_margin,
                    "withdrawable": withdrawable,
                    "positions": positions,
                },
            )
        )
        db.commit()

    return {
        "status": "ok" if api_error is None and account_error is None else "degraded",
        "network": settings.hyperliquid_network,
        "checked_at": checked_at,
        "api": {
            "status": "ok" if api_error is None else "error",
            "latency_ms": api_latency_ms,
            "info_url": settings.info_url,
            "ws_url": settings.ws_url,
            "universe_count": len((meta or {}).get("universe", [])),
            "mids_count": len(mids or {}),
            "error": api_error,
        },
        "account": {
            "configured": bool(address),
            "address": _redact_address(address),
            "account_value": _string_decimal(margin.get("accountValue")),
            "effective_account_value": str(effective_account_value.quantize(Decimal("0.000001"))),
            "free_spot_usdc": str(free_spot_usdc.quantize(Decimal("0.000001"))),
            "withdrawable": _string_decimal(withdrawable),
            "total_margin_used": _string_decimal(margin.get("totalMarginUsed")),
            "total_notional_position": _string_decimal(margin.get("totalNtlPos")),
            "cross_ntl_pos": _string_decimal(cross_margin.get("totalNtlPos")),
            "cross_account_value": _string_decimal(cross_margin.get("accountValue")),
            "cross_margin_used": _string_decimal(cross_margin.get("totalMarginUsed")),
            "cross_maintenance_margin_used": _string_decimal((user_state or {}).get("crossMaintenanceMarginUsed")),
            "unified_account_ratio_pct": quantized_decimal(uar.pct),
            "unified_account_ratio_status": uar.status,
            "unified_account_ratio_source": uar.source,
            "unified_account_ratio_threshold_pct": quantized_decimal(uar.threshold_pct),
            "unified_account_ratio_numerator": quantized_decimal(uar.numerator),
            "unified_account_ratio_denominator": quantized_decimal(uar.denominator),
            "open_positions": len(positions),
            "open_orders": len(open_orders),
            "error": account_error,
        },
        "positions": positions,
        "spot_balances": spot_balances,
        "open_orders": open_orders[:50],
        "funding": {
            "status": "ok" if predicted_error is None else "error",
            "predicted": relevant_fundings,
            "user_last_24h": user_funding[:50],
            "error": predicted_error,
        },
        "last_snapshot": _latest_account_snapshot(db),
    }


@router.post("/account/orders/{oid}/cancel")
def cancel_account_order(
    oid: int,
    coin: str = Query(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-/]+$"),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    live_settings = effective_live_execution_settings(db, settings)
    try:
        response = HyperliquidTradingClient(settings).cancel_order(
            coin=coin,
            oid=oid,
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(
        DecisionLog(
            event_type="live_order_cancel_requested",
            message="Open order cancel requested from account monitor",
            data={"coin": coin, "oid": oid, "response": response},
        )
    )
    db.commit()
    return {"status": "cancel_requested", "coin": coin, "oid": oid, "response": response}


@router.post("/account/positions/{coin}/close")
def close_account_position(
    coin: str = Path(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-/]+$"),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    live_settings = effective_live_execution_settings(db, settings)
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    trading = HyperliquidTradingClient(settings)
    size: Decimal | None = None
    try:
        remote = _account_position_for_coin(trading.user_state(address), coin)
        if remote is None:
            raise HTTPException(status_code=404, detail="Position not found on Hyperliquid")
        size = _decimal(remote.get("szi"))
        if size is None or size == 0:
            raise HTTPException(status_code=409, detail="Position has no open size")
        result = trading.submit_market_close(
            coin=coin,
            is_buy=size < 0,
            size=abs(size),
            max_slippage_pct=float(live_settings["max_slippage_pct"]),
            expires_after_seconds=int(live_settings["order_expires_after_seconds"]),
        )
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if size is None:
        raise HTTPException(status_code=409, detail="Position has no open size")
    from sqlalchemy import or_
    open_positions = (
        db.query(Position)
        .join(Position.market)
        .filter(
            Position.status == "open",
            Position.mode == "live",
            or_(
                Market.symbol == coin,
                Market.metadata_json["hl_coin"].astext == coin,
            ),
        )
        .all()
    )
    exit_price = result.avg_price or Decimal(str(abs(size)))
    executor = OrderExecutionEngine(settings)
    for pos in open_positions:
        executor.close_position(db, pos, exit_price=exit_price, reason="manual", commit=False)
    db.add(
        DecisionLog(
            event_type="live_account_position_close_requested",
            message="Remote account position close requested from account monitor",
            data={
                "coin": coin,
                "side": "long" if size > 0 else "short",
                "size": str(abs(size)),
                "result": result.raw_response,
                "oid": result.oid,
                "trado_positions_closed": [pos.id for pos in open_positions],
            },
        )
    )
    db.commit()
    return {
        "status": result.status,
        "coin": coin,
        "oid": result.oid,
        "avg_price": str(result.avg_price) if result.avg_price is not None else None,
        "filled_size": str(result.filled_size) if result.filled_size is not None else None,
    }


@router.post("/account/positions/patch-missing-exit-data")
def patch_missing_exit_data(db: Session = Depends(get_db)) -> dict:
    """Reconcile exit data for closed live positions using HL fill history.

    Three cases are handled in a single pass:
    1. realized_pnl IS NULL (e.g. UNI: no SL order in DB, no fill matched).
    2. A filled exit order exists whose oid is NOT in HL fills — data is wrong
       (e.g. BTC: reconciliation matched the wrong fill; actual close was a
       manual market order). Overrides with the real HL close fill.
    3. exit_price missing from metadata — written from the last filled exit
       order without changing realized_pnl.
    """
    settings = get_settings()
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    from app.services.live_reconciliation import _index_fills, _index_fills_by_coin, _find_close_fill
    from app.engine.execution import coin_for_market
    trading = HyperliquidTradingClient(settings)
    try:
        raw_fills = trading.user_fills(address)
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _, fills_by_oid = _index_fills(raw_fills or [])
    fills_by_coin = _index_fills_by_coin(raw_fills or [])
    all_closed = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.mode == "live", Position.status == "closed")
        .all()
    )
    patched = []
    for pos in all_closed:
        if pos.market is None:
            continue
        try:
            coin = coin_for_market(pos.market)
        except ValueError:
            continue
        metadata = dict(pos.metadata_json or {})
        exit_orders = (
            db.query(Order)
            .filter(
                Order.position_id == pos.id,
                Order.order_type.in_(["tp1", "tp2", "tp3", "stop_loss", "manual"]),
                Order.status == "filled",
            )
            .order_by(Order.id.desc())
            .all()
        )
        last_exit = exit_orders[0] if exit_orders else None

        # Case 2: filled exit order whose oid is not in HL fills → wrong data
        wrong_data = False
        if last_exit is not None and last_exit.hyperliquid_order_id:
            try:
                oid_int = int(last_exit.hyperliquid_order_id)
                wrong_data = oid_int not in fills_by_oid
            except (TypeError, ValueError):
                pass

        # Case 1: no pnl at all; Case 2: wrong data from bad fill match
        if pos.realized_pnl is None or wrong_data:
            hl_fill = _find_close_fill(coin, pos.opened_at, fills_by_coin)
            if hl_fill is None:
                continue
            hl_px = _decimal(hl_fill.get("px"))
            hl_pnl = _decimal(hl_fill.get("closedPnl"))
            if hl_px is None or hl_pnl is None:
                continue
            pos.realized_pnl = hl_pnl
            metadata["realized_pnl"] = str(hl_pnl)
            metadata["exit_price"] = str(hl_px)
            metadata["exit_reason"] = "hl_fill_patch"
            # If the exit order was incorrectly marked as filled, correct it
            if wrong_data and last_exit is not None:
                last_exit.status = "canceled"
                last_exit.raw_response = {**(last_exit.raw_response or {}), "patched_to_canceled": True}
                db.add(last_exit)
            pos.metadata_json = metadata
            db.add(pos)
            db.add(DecisionLog(
                event_type="live_position_exit_data_patched",
                market_id=pos.market_id,
                position_id=pos.id,
                message="Exit data reconciled from HL fill history",
                data={"coin": coin, "exit_price": str(hl_px), "realized_pnl": str(hl_pnl),
                      "was_wrong_data": wrong_data},
            ))
            patched.append({"position_id": pos.id, "coin": coin,
                            "exit_price": str(hl_px), "realized_pnl": str(hl_pnl),
                            "was_wrong_data": wrong_data})
            continue

        # Case 3: pnl is correct but exit_price missing from metadata
        if not metadata.get("exit_price") and last_exit is not None and last_exit.price is not None:
            metadata["exit_price"] = str(last_exit.price)
            pos.metadata_json = metadata
            db.add(pos)
            patched.append({"position_id": pos.id, "coin": coin,
                            "exit_price": str(last_exit.price), "realized_pnl": None,
                            "was_wrong_data": False})
    db.commit()
    return {"patched": len(patched), "positions": patched}


@router.post("/account/positions/{coin}/repair-protection")
def repair_position_protection(
    coin: str,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    address = settings.hyperliquid_account_address
    if not address:
        raise HTTPException(status_code=400, detail="HYPERLIQUID_ACCOUNT_ADDRESS is required")
    trading = HyperliquidTradingClient(settings)
    from app.services.live_reconciliation import (
        _index_frontend_open_orders,
        protection_state_for_live_position,
        reconcile_live_positions,
    )

    updated = reconcile_live_positions(db, trading_client=trading, coins={coin}, force_repair=True)
    local_positions = (
        db.query(Position)
        .join(Position.market)
        .options(joinedload(Position.market))
        .filter(
            Position.mode == "live",
            Position.status == "open",
            (Market.symbol == coin) | (Market.metadata_json["hl_coin"].astext == coin),
        )
        .all()
    )
    if not local_positions:
        raise HTTPException(status_code=404, detail=f"No open Trado live position found for {coin}")
    try:
        frontend_open_orders = trading.frontend_open_orders(address)
    except HyperliquidError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    frontend_by_coin = _index_frontend_open_orders(frontend_open_orders)
    states = [
        protection_state_for_live_position(
            db,
            position,
            frontend_by_coin.get(coin, []),
        )
        for position in local_positions
    ]
    aggregated = _merge_protection_states(states)
    db.add(
        DecisionLog(
            event_type="live_protection_manual_repair_requested",
            message="Manual protection repair requested from account monitor",
            data={
                "coin": coin,
                "updated_positions": updated,
                "position_ids": aggregated.get("local_position_ids", []),
                "status": aggregated.get("status"),
                "issues": aggregated.get("issues", []),
            },
        )
    )
    db.commit()
    return {
        "coin": coin,
        "updated_positions": updated,
        "protection": aggregated,
    }


@router.get("/logs")
def logs(
    db: Session = Depends(get_db),
    signal_id: int | None = Query(None),
    position_id: int | None = Query(None),
    market_id: int | None = Query(None),
    event_type: str | None = Query(None),
    limit: int = Query(250, ge=1, le=1000),
) -> list[dict]:
    q = db.query(DecisionLog)
    if signal_id is not None:
        q = q.filter(DecisionLog.signal_id == signal_id)
    if position_id is not None:
        q = q.filter(DecisionLog.position_id == position_id)
    if market_id is not None:
        q = q.filter(DecisionLog.market_id == market_id)
    if event_type is not None:
        q = q.filter(DecisionLog.event_type == event_type)
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "market_id": row.market_id,
            "signal_id": row.signal_id,
            "position_id": row.position_id,
            "message": row.message,
            "data": row.data,
        }
        for row in q.order_by(DecisionLog.timestamp.desc()).limit(limit).all()
    ]


@router.post("/push/subscribe")
def push_subscribe(payload: PushSubscribePayload, db: Session = Depends(get_db)) -> dict:
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == payload.endpoint).first()
    if sub is None:
        sub = PushSubscription(
            endpoint=payload.endpoint,
            p256dh=payload.keys.get("p256dh", ""),
            auth=payload.keys.get("auth", ""),
        )
        db.add(sub)
    else:
        sub.active = True
        sub.p256dh = payload.keys.get("p256dh", sub.p256dh)
        sub.auth = payload.keys.get("auth", sub.auth)
    db.commit()
    return {"status": "subscribed"}


@router.delete("/push/subscribe")
def push_unsubscribe(payload: PushUnsubscribePayload, db: Session = Depends(get_db)) -> dict:
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == payload.endpoint).first()
    if sub is not None:
        sub.active = False
        db.commit()
    return {"status": "unsubscribed"}


@router.post("/push/test")
async def push_test(db: Session = Depends(get_db)) -> dict:
    from app.notifications.push import WebPushNotifier
    sent = await WebPushNotifier(get_settings()).send(db, "Trado", "Browser push funzionante!")
    return {"status": "sent", "count": sent}


@router.get("/learn/trade-management", include_in_schema=False, response_class=HTMLResponse)
async def trade_management_guide() -> HTMLResponse:
    return HTMLResponse(_TRADE_MANAGEMENT_GUIDE_HTML)


@router.post("/telegram/test-close-card")
async def telegram_test_close_card() -> dict:
    """Invia una card di test sui canali Telegram configurati."""
    from app.notifications.card_renderer import render_close_card
    from app.notifications.telegram import TelegramNotifier, _build_live_close_message

    settings = get_settings()
    notifier = TelegramNotifier(settings)

    symbol = "BTC"
    side = "LONG"
    entry = Decimal("95000")
    exit_p = Decimal("101650")
    pnl_usd = Decimal("332.50")
    pnl_pct = 7.0
    leverage = Decimal("3")
    duration = 14400  # 4h

    text = _build_live_close_message(
        symbol=symbol,
        side=side,
        reason="tp2",
        entry_price=entry,
        exit_price=exit_p,
        remaining_size=None,
        step_pnl=None,
        total_pnl=pnl_usd,
        is_partial=False,
    )

    png = render_close_card(
        symbol=symbol,
        side=side,
        leverage=leverage,
        entry_price=entry,
        exit_price=exit_p,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        duration_seconds=duration,
        reason="tp2",
    )

    channels: list[str] = []
    if settings.telegram_chat_id:
        ok = await notifier._send_photo(png, text)
        channels.append(f"main({'ok' if ok else 'fail'})")
    if settings.telegram_signal_chat_id:
        ok = await notifier._send_photo(png, text, chat_id=settings.telegram_signal_chat_id)
        channels.append(f"signal({'ok' if ok else 'fail'})")

    if not channels:
        return {"status": "skipped", "reason": "nessun chat_id configurato"}
    return {"status": "sent", "channels": channels}


@router.post("/telegram/channel-strategy-message")
async def telegram_channel_strategy_message(pin: bool = True) -> dict:
    """Pubblica nel canale segnali il messaggio bilingue di strategia e,
    opzionalmente, lo pinna."""
    from app.notifications.telegram import TelegramNotifier

    notifier = TelegramNotifier(get_settings())
    return await notifier.send_channel_strategy_message(pin=pin)
