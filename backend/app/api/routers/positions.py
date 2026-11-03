"""Posizioni aperte/storiche e ordini."""
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.api.routers.common import _serialize_order, _serialize_position
from app.core.config import get_settings
from app.db.models import (
    DecisionLog,
    Market,
    Order,
    Position,
    Signal,
)
from app.db.session import get_db
from app.engine.execution import OrderExecutionEngine

_logger = structlog.get_logger()

router = APIRouter()


# Beyond this age, Position.unrealized_pnl is considered stale and the API
# recomputes it on the fly from the latest candle, instead of trusting the
# worker-written value.
POSITION_HISTORY_LIMIT = 250
ORDER_HISTORY_LIMIT = 250
EXIT_ORDER_TYPES = (
    "tp1",
    "tp2",
    "tp3",
    "take_profit",
    "stop_loss",
    "manual",
    "the_bull_profit_update",
    "the_bull_close_update",
    "the_bull_stop_update",
)
HISTORY_ORDER_TYPES = (
    *EXIT_ORDER_TYPES,
    "time_stop",
    "btc_macro_protection_loss_exit",
    "liquidation_guard_failed",
)
@router.get("/positions")
def positions(db: Session = Depends(get_db)) -> list[dict]:
    open_rows = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.status == "open")
        .order_by(Position.opened_at.desc())
        .all()
    )
    remaining_history_limit = max(POSITION_HISTORY_LIMIT - len(open_rows), 0)
    history_rows = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.status != "open")
        .order_by(Position.opened_at.desc())
        .limit(remaining_history_limit)
        .all()
        if remaining_history_limit > 0
        else []
    )
    rows = sorted([*open_rows, *history_rows], key=lambda row: row.opened_at, reverse=True)
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

    now_utc = datetime.now(UTC)
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
        cutoff = datetime.now(UTC) - {
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

    now_utc = datetime.now(UTC)
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
