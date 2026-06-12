"""Helper condivisi tra i router: serializzatori, utility datetime/decimal,
redazione errori e arricchimento segnali."""
import contextlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.db.models import (
    Candle,
    DecisionLog,
    Market,
    Order,
    Position,
    Signal,
    StrategyConfig,
)
from app.engine.score_calibration import (
    cached_calibration_table,
)
from app.schemas import (
    SignalOut,
)
from app.services.position_pnl import calculate_unrealized_pnl as _calculate_unrealized_pnl
from app.services.trade_metrics import build_trade_metrics
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.registry import strategy_registry

# Beyond this age, Position.unrealized_pnl is considered stale and the API
# recomputes it on the fly from the latest candle, instead of trusting the
# worker-written value.
PNL_STALENESS = timedelta(minutes=5)

_logger = structlog.get_logger()


def _log_and_redact(event: str, exc: Exception) -> str:
    """Log the full exception server-side and return a safe error_id to embed
    in the API response. Callers never echo exception messages anymore."""
    error_id = uuid.uuid4().hex[:12]
    _logger.exception(event, error_id=error_id, error=str(exc))
    return error_id

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

    now_utc = now_utc or datetime.now(UTC)
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
                with contextlib.suppress(Exception):
                    unrealized_pnl = _calculate_unrealized_pnl(row, Decimal(str(mark_str)))
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


def _serialize_decision_log(row: DecisionLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "event_type": row.event_type,
        "market_id": row.market_id,
        "signal_id": row.signal_id,
        "position_id": row.position_id,
        "message": row.message,
        "data": row.data,
    }


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _max_datetime(*values: datetime | None) -> datetime | None:
    parsed = [value for value in values if value is not None]
    return max(parsed) if parsed else None


def _latest_decision_log(db: Session, event_types: Sequence[str]) -> DecisionLog | None:
    if not event_types:
        return None
    return (
        db.query(DecisionLog)
        .filter(DecisionLog.event_type.in_(tuple(event_types)))
        .order_by(DecisionLog.timestamp.desc())
        .first()
    )


def _decision_log_count_since(db: Session, event_types: Sequence[str], since: datetime) -> int:
    if not event_types:
        return 0
    return (
        db.query(DecisionLog)
        .filter(
            DecisionLog.event_type.in_(tuple(event_types)),
            DecisionLog.timestamp >= since,
        )
        .count()
    )


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


