"""Segnali: lista, dettaglio, snapshot, azioni (ignore/reopen/delete/execute)."""
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.api.routers.common import (
    _enrich_signal_with_calibration,
    _serialize_decision_log,
    _serialize_position,
)
from app.core.config import get_settings
from app.db.models import (
    Alert,
    AppSetting,
    Candle,
    DecisionLog,
    Indicator,
    Order,
    Position,
    Signal,
)
from app.db.session import get_db
from app.engine.execution import OrderExecutionEngine
from app.schemas import (
    CandleOut,
    DecisionLogOut,
    IndicatorOut,
    SignalOut,
    SignalSnapshotOut,
)

_logger = structlog.get_logger()

router = APIRouter()


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


@router.get("/signals/{signal_id}/snapshot", response_model=SignalSnapshotOut)
def signal_snapshot(signal_id: int, db: Session = Depends(get_db)) -> SignalSnapshotOut:
    signal = db.query(Signal).options(joinedload(Signal.market)).filter(Signal.id == signal_id).first()
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    source_time = signal.source_candle_open_time
    candles: list[Candle] = []
    indicators: list[IndicatorOut] = []

    if source_time is not None:
        before = (
            db.query(Candle)
            .filter(
                Candle.market_id == signal.market_id,
                Candle.timeframe == signal.timeframe,
                Candle.open_time <= source_time,
            )
            .order_by(Candle.open_time.desc())
            .limit(160)
            .all()
        )
        after = (
            db.query(Candle)
            .filter(
                Candle.market_id == signal.market_id,
                Candle.timeframe == signal.timeframe,
                Candle.open_time > source_time,
            )
            .order_by(Candle.open_time.asc())
            .limit(40)
            .all()
        )
        candles = [*reversed(before), *after]

        indicator_row = (
            db.query(Indicator)
            .filter(
                Indicator.market_id == signal.market_id,
                Indicator.timeframe == signal.timeframe,
                Indicator.timestamp == source_time,
            )
            .first()
        )
        if indicator_row is not None:
            indicators = [
                IndicatorOut(
                    indicator_name=name,
                    timeframe=indicator_row.timeframe,
                    timestamp=indicator_row.timestamp,
                    values=vals,
                )
                for name, vals in sorted(indicator_row.values.items())
            ]

    candidate_positions = (
        db.query(Position)
        .options(joinedload(Position.market))
        .filter(Position.market_id == signal.market_id)
        .order_by(Position.opened_at.desc())
        .all()
    )
    linked_positions = [
        row
        for row in candidate_positions
        if str((row.metadata_json or {}).get("source_signal_id")) == str(signal.id)
    ]
    position_ids = [row.id for row in linked_positions]
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

    logs = (
        db.query(DecisionLog)
        .filter(DecisionLog.signal_id == signal.id)
        .order_by(DecisionLog.timestamp.desc())
        .limit(100)
        .all()
    )

    now_utc = datetime.now(UTC)
    return SignalSnapshotOut(
        signal=_enrich_signal_with_calibration(signal, db),
        source_candle_open_time=source_time,
        candles=[CandleOut.model_validate(c) for c in candles],
        indicators=indicators,
        positions=[
            _serialize_position(
                db,
                row,
                orders_by_position.get(row.id, []),
                now_utc=now_utc,
            )
            for row in linked_positions
        ],
        decision_logs=[DecisionLogOut(**_serialize_decision_log(row)) for row in logs],
    )


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


