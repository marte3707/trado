import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
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

# Numero massimo di fetch HTTP candele eseguiti in parallelo durante un prefetch.
# Moderato di proposito: il client HTTP gestisce 429/5xx con backoff esponenziale
# e un circuit breaker condiviso, ma evitiamo comunque di saturare l'endpoint info
# di Hyperliquid con centinaia di richieste simultanee.
DEFAULT_PREFETCH_CONCURRENCY = 4


def _compute_start_ms(
    latest_open: datetime | None, interval_ms: int, lookback: int, end_ms: int
) -> int:
    """Start della finestra di fetch: incrementale dall'ultima candela nota (con un
    bar di overlap per far rinfrescare l'ultima barra), oppure lookback pieno al
    primo sync di una coppia (market, timeframe)."""
    if latest_open is not None:
        latest_ms = int(latest_open.timestamp() * 1000)
        return latest_ms - interval_ms
    return end_ms - interval_ms * lookback


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
        """Sync sincrono di una singola coppia: query latest_open → fetch HTTP →
        persist. Mantenuto per i percorsi che sincronizzano una coppia per volta
        (es. test). Il worker usa `prefetch` + `persist_candles` per parallelizzare
        i fetch HTTP su molte coppie."""
        now = datetime.now(timezone.utc)
        interval_ms = TIMEFRAME_MS[timeframe]
        end_ms = int(now.timestamp() * 1000)
        latest_open = (
            db.query(Candle.open_time)
            .filter(Candle.market_id == market.id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(1)
            .scalar()
        )
        start_ms = _compute_start_ms(latest_open, interval_ms, lookback, end_ms)
        rows = await self.client.candles(
            coin=market.metadata_json.get("hl_coin", market.symbol),
            interval=timeframe,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return self.persist_candles(db, market, timeframe, rows, open_interest=open_interest)

    def persist_candles(
        self,
        db: Session,
        market: Market,
        timeframe: str,
        rows: list[dict],
        open_interest: float | None = None,
    ) -> int:
        """Scrive (upsert) le candele chiuse di una coppia e committa. Sincrono:
        nel worker viene invocato via `asyncio.to_thread` sulla sessione condivisa,
        coerentemente con gli altri accessi DB serializzati del ciclo."""
        now = datetime.now(timezone.utc)
        interval_ms = TIMEFRAME_MS[timeframe]
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

    def _latest_open_map(
        self,
        db: Session,
        market_ids: list[int],
        timeframes: list[str],
    ) -> dict[tuple[int, str], datetime]:
        """Una sola query per l'ultima `open_time` di ogni coppia (market, timeframe),
        invece di una query per coppia. Alimenta il calcolo di start_ms del prefetch."""
        if not market_ids or not timeframes:
            return {}
        rows = (
            db.query(Candle.market_id, Candle.timeframe, func.max(Candle.open_time))
            .filter(Candle.market_id.in_(market_ids), Candle.timeframe.in_(timeframes))
            .group_by(Candle.market_id, Candle.timeframe)
            .all()
        )
        return {(market_id, timeframe): latest for market_id, timeframe, latest in rows}

    async def prefetch(
        self,
        db: Session,
        pairs: list[tuple[Market, str]],
        lookback: int = DEFAULT_CANDLE_LOOKBACK,
        concurrency: int = DEFAULT_PREFETCH_CONCURRENCY,
    ) -> dict[tuple[int, str], list[dict] | Exception]:
        """Esegue in parallelo (con concorrenza limitata) i fetch HTTP delle candele
        per molte coppie (market, timeframe). Non scrive su DB: ritorna i rows grezzi
        (o l'eccezione catturata) per coppia, lasciando la persistenza serializzata al
        chiamante. La query per le ultime open_time è batchata in un'unica SELECT."""
        if not pairs:
            return {}
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        market_ids = list({market.id for market, _ in pairs})
        timeframes = list({timeframe for _, timeframe in pairs})
        latest_map = await asyncio.to_thread(
            self._latest_open_map, db, market_ids, timeframes
        )
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _fetch(
            market: Market, timeframe: str
        ) -> tuple[tuple[int, str], list[dict] | Exception]:
            key = (market.id, timeframe)
            interval_ms = TIMEFRAME_MS[timeframe]
            start_ms = _compute_start_ms(latest_map.get(key), interval_ms, lookback, end_ms)
            async with semaphore:
                try:
                    rows = await self.client.candles(
                        coin=market.metadata_json.get("hl_coin", market.symbol),
                        interval=timeframe,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                    return key, rows
                except Exception as exc:  # noqa: BLE001 — propagata al chiamante per coppia
                    return key, exc

        results = await asyncio.gather(
            *(_fetch(market, timeframe) for market, timeframe in pairs)
        )
        return dict(results)
