"""Retention automatica dei dati storici.

Mantiene bounded la crescita di candles e indicators eliminando righe
oltre le finestre per timeframe. Chiamato dal worker una volta al giorno.

Finestre (abbondanti rispetto al lookback operativo massimo di 500 bar):
  5m  → 30 giorni  (≈8.640 bar/mercato)
  15m → 30 giorni  (≈2.880 bar/mercato)
  30m → 60 giorni  (≈2.880 bar/mercato)
  1h  → 90 giorni  (≈2.160 bar/mercato)
  4h  → 180 giorni (≈1.080 bar/mercato)
  1d  → 730 giorni (≈730  bar/mercato)
"""

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger()

# (timeframe, giorni di retention)
_RETENTION_DAYS: list[tuple[str, int]] = [
    ("5m", 30),
    ("15m", 30),
    ("30m", 60),
    ("1h", 90),
    ("4h", 180),
    ("1d", 730),
]


def prune_old_data(db: Session) -> dict[str, int]:
    """Elimina candele e indicatori oltre le finestre di retention.

    Restituisce un dict con il conteggio delle righe eliminate per tabella.
    """
    deleted_candles = 0
    deleted_indicators = 0

    for timeframe, days in _RETENTION_DAYS:
        cutoff = f"NOW() - INTERVAL '{days} days'"

        deleted_candles += db.execute(  # type: ignore[attr-defined]
            text(
                f"DELETE FROM candles "
                f"WHERE timeframe = :tf AND open_time < {cutoff}"
            ),
            {"tf": timeframe},
        ).rowcount

        deleted_indicators += db.execute(  # type: ignore[attr-defined]
            text(
                f"DELETE FROM indicators "
                f"WHERE timeframe = :tf AND timestamp < {cutoff}"
            ),
            {"tf": timeframe},
        ).rowcount

    db.commit()

    logger.info(
        "data_retention_completed",
        deleted_candles=deleted_candles,
        deleted_indicators=deleted_indicators,
    )
    return {"candles": deleted_candles, "indicators": deleted_indicators}
