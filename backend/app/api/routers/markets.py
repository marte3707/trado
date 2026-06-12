"""Mercati, candele, indicatori e market intelligence (CMC/CoinGecko)."""
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routers.common import _computed_indicator_snapshots
from app.core.config import get_settings
from app.db.models import (
    Candle,
    Indicator,
    Market,
)
from app.db.session import get_db
from app.schemas import (
    CandleOut,
    IndicatorOut,
    MarketOut,
)
from app.services.candle_sync import TIMEFRAME_MS
from app.services.coinmarketcap import CoinMarketCapClient
from app.services.hyperliquid import (
    HyperliquidClient,
    parse_hl_candle,
)
from app.services.market_dashboard import MarketDashboardService
from app.services.market_sync import MarketSyncService

_logger = structlog.get_logger()

router = APIRouter()


_market_data_client: CoinMarketCapClient | None = None
_market_dashboard_service: MarketDashboardService | None = None
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
            now = datetime.now(UTC)
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


