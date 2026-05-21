from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Candle, Market
from app.services.hyperliquid import HyperliquidClient, parse_hl_candle


TIMEFRAME_MS = {
    "1m": 60 * 1000,
    "3m": 3 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "8h": 8 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

# Number of candles to backfill the first time we sync a (market, timeframe)
# pair (no rows in DB yet). Subsequent syncs are incremental.
DEFAULT_CANDLE_LOOKBACK = 300


class CandleSyncService:
    def __init__(self) -> None:
        self.client = HyperliquidClient(get_settings())

    async def sync_market(
        self,
        db: Session,
        market: Market,
        timeframe: str,
        lookback: int = DEFAULT_CANDLE_LOOKBACK,
        open_interest: float | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        interval_ms = TIMEFRAME_MS[timeframe]
        end_ms = int(now.timestamp() * 1000)
        # Incremental sync: if we already have candles for this (market, timeframe),
        # only fetch from the last stored open_time (with a one-bar overlap to
        # let on_conflict refresh the most recent bar). Falls back to the full
        # `lookback` window only when the table is empty for this pair.
        latest_open = (
            db.query(Candle.open_time)
            .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(1)
            .scalar()
        )
        if latest_open is not None:
            latest_ms = int(latest_open.timestamp() * 1000)
            start_ms = latest_ms - interval_ms
        else:
            start_ms = end_ms - interval_ms * lookback
        rows = await self.client.candles(
            coin=market.metadata_json.get("hl_coin", market.symbol),
            interval=timeframe,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        cutoff = now - timedelta(milliseconds=interval_ms)
        inserted = 0
        for row in rows:
            parsed = parse_hl_candle(row)
            if parsed["open_time"] > cutoff:
                continue
            statement = insert(Candle).values(
                market_id=market.id,
                timeframe=timeframe,
                source="hyperliquid",
                **parsed,
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_candle_market_tf_time",
                set_={
                    "open": parsed["open"],
                    "high": parsed["high"],
                    "low": parsed["low"],
                    "close": parsed["close"],
                    "volume": parsed["volume"],
                    "source": "hyperliquid",
                },
            )
            db.execute(statement)
            inserted += 1
        if open_interest is not None:
            latest = (
                db.query(Candle)
                .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.desc())
                .first()
            )
            if latest is not None:
                latest.open_interest = open_interest
        db.commit()
        return inserted
