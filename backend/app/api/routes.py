from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.core.runtime_settings import effective_live_trading_enabled, effective_operation_mode
from app.db.models import Alert, AppSetting, Candle, DecisionLog, Indicator, Market, Order, Position, PushSubscription, Signal, StrategyConfig
from app.db.session import get_db
from app.engine.calibration import AutoCalibrationEngine, serialize_calibration
from app.engine.execution import OrderExecutionEngine
from app.engine.performance import SignalPerformanceAnalyzer, serialize_stats
from app.schemas import CandleOut, IndicatorOut, MarketOut, PushSubscribePayload, PushUnsubscribePayload, SettingsPatch, SignalOut, StrategyPatch
from app.services.coinmarketcap import CoinMarketCapClient
from app.services.market_sync import MarketSyncService
from app.strategies.base import CandleInput, StrategyContext
from app.strategies.registry import strategy_registry

router = APIRouter(prefix="/api")
_market_data_client: CoinMarketCapClient | None = None


def market_data_client() -> CoinMarketCapClient:
    global _market_data_client
    if _market_data_client is None:
        _market_data_client = CoinMarketCapClient(get_settings())
    return _market_data_client


def _calculate_unrealized_pnl(position: Position, mark_price: Decimal | None) -> Decimal | None:
    if mark_price is None:
        return None
    side = position.side.lower()
    if side == "long":
        return (mark_price - position.entry_price) * position.size
    if side == "short":
        return (position.entry_price - mark_price) * position.size
    return None


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


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    mode = effective_operation_mode(db, settings)
    enable_live_trading = effective_live_trading_enabled(db, settings)
    return {
        "status": "ok",
        "mode": mode.value,
        "network": settings.hyperliquid_network,
        "enable_live_trading": enable_live_trading,
        "live_trading_allowed": mode.value == "AUTO_TRADE" and enable_live_trading,
    }


@router.get("/market-intelligence")
def market_intelligence() -> dict:
    client = market_data_client()
    fear_greed = client.latest_fear_greed()
    altcoin_season = client.latest_altcoin_season()
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
    }


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
def candles(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    market_type: str | None = None,
    limit: int = Query(300, le=1000),
    db: Session = Depends(get_db),
) -> list[Candle]:
    market_query = db.query(Market).filter(Market.symbol == symbol)
    if market_type:
        market_query = market_query.filter(Market.market_type == market_type)
    market = market_query.first()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return (
        db.query(Candle)
        .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
        .order_by(Candle.open_time.desc())
        .limit(limit)
        .all()
    )[::-1]


@router.get("/indicators", response_model=list[IndicatorOut])
def indicators(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    market_type: str | None = None,
    db: Session = Depends(get_db),
) -> list[Indicator] | list[dict]:
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
    return (
        db.query(Indicator)
        .filter(
            Indicator.market_id == market.id,
            Indicator.timeframe == timeframe,
            Indicator.timestamp == latest[0],
        )
        .order_by(Indicator.indicator_name)
        .all()
    )


@router.get("/signals", response_model=list[SignalOut])
def signals(
    status: str | None = None,
    min_score: int | None = None,
    direction: str | None = None,
    timeframe: str | None = None,
    db: Session = Depends(get_db),
) -> list[Signal]:
    query = db.query(Signal).options(joinedload(Signal.market)).order_by(Signal.created_at.desc())
    if status:
        query = query.filter(Signal.status == status)
    if min_score is not None:
        query = query.filter(Signal.score >= min_score)
    if direction:
        query = query.filter(Signal.direction == direction)
    if timeframe:
        query = query.filter(Signal.timeframe == timeframe)
    return query.limit(250).all()


@router.get("/signals/{signal_id}", response_model=SignalOut)
def signal_detail(signal_id: int, db: Session = Depends(get_db)) -> Signal:
    signal = db.query(Signal).options(joinedload(Signal.market)).filter(Signal.id == signal_id).first()
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal


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
    position = OrderExecutionEngine(get_settings()).execute_paper(db, signal)
    return {"position_id": position.id, "status": "opened"}


@router.post("/signals/{signal_id}/execute-live")
def execute_live(signal_id: int, db: Session = Depends(get_db)) -> dict:
    signal = db.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.status != "new":
        raise HTTPException(status_code=409, detail=f"Signal is already {signal.status}")
    try:
        OrderExecutionEngine(get_settings()).execute_live(db, signal)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return {"status": "submitted"}


@router.get("/positions")
def positions(db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(Position)
        .options(joinedload(Position.market))
        .order_by(Position.opened_at.desc())
        .limit(250)
        .all()
    )
    result = []
    for row in rows:
        live_pnl = None
        if row.status == "open":
            latest_candle = _latest_candle_for_position(db, row)
            live_pnl = _calculate_unrealized_pnl(row, latest_candle.close if latest_candle else None)
        unrealized_pnl = live_pnl if live_pnl is not None else row.unrealized_pnl
        result.append(
            {
                "id": row.id,
                "market_id": row.market_id,
                "market": {
                    "id": row.market.id,
                    "exchange": row.market.exchange,
                    "symbol": row.market.symbol,
                    "base": row.market.base,
                    "quote": row.market.quote,
                    "market_type": row.market.market_type,
                    "active": row.market.active,
                    "metadata_json": row.market.metadata_json,
                },
                "mode": row.mode,
                "side": row.side,
                "entry_price": str(row.entry_price),
                "size": str(row.size),
                "stop_loss": str(row.stop_loss) if row.stop_loss is not None else None,
                "take_profit": str(row.take_profit) if row.take_profit is not None else None,
                "status": row.status,
                "opened_at": row.opened_at,
                "closed_at": row.closed_at,
                "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
                "unrealized_pnl": str(unrealized_pnl) if unrealized_pnl is not None else None,
                "metadata_json": row.metadata_json,
            }
        )
    return result


@router.post("/positions/{position_id}/close")
def close_position(position_id: int, db: Session = Depends(get_db)) -> dict:
    position = db.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "open":
        return {"status": position.status}
    OrderExecutionEngine(get_settings()).close_position(db, position, position.entry_price, "manual")
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
    rows = db.query(Order).order_by(Order.created_at.desc()).limit(250).all()
    signal_ids = {row.signal_id for row in rows if row.signal_id is not None}
    position_ids = {row.position_id for row in rows if row.position_id is not None}
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
            .filter(Position.id.in_(position_ids))
            .all()
        }
        if position_ids
        else {}
    )

    def serialize_market(market: Market | None) -> dict | None:
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

    result = []
    for row in rows:
        position = positions_by_id.get(row.position_id) if row.position_id is not None else None
        signal = signals_by_id.get(row.signal_id) if row.signal_id is not None else None
        result.append(
            {
                "id": row.id,
                "position_id": row.position_id,
                "signal_id": row.signal_id,
                "market": serialize_market(position.market if position else signal.market if signal else None),
                "mode": row.mode,
                "order_type": row.order_type,
                "side": row.side,
                "price": str(row.price) if row.price is not None else None,
                "size": str(row.size),
                "status": row.status,
                "created_at": row.created_at,
            }
        )
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
        try:
            signal = signals_by_id.get(int(metadata.get("source_signal_id")))
        except (TypeError, ValueError):
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


@router.get("/account/snapshot")
def account_snapshot() -> dict:
    return {"status": "not_configured", "message": "Account snapshots are collected by worker when address is set"}


@router.get("/logs")
def logs(db: Session = Depends(get_db)) -> list[dict]:
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "message": row.message,
            "data": row.data,
        }
        for row in db.query(DecisionLog).order_by(DecisionLog.timestamp.desc()).limit(250).all()
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
