"""indicators: consolida righe per timestamp e rimuove indici ridondanti

Riduce la tabella indicators da N righe per (market, tf, timestamp) a 1 sola:
  values = { "rsi": {...}, "macd": {...}, ... }

Elimina anche gli indici duplicati/ridondanti su candles (ix_candles_market_tf_time
è identico alla unique constraint uq_candle_market_tf_time).

Revision ID: 0009_indicators_consolidate
Revises: 0008_bot_users_trade_capital
Create Date: 2026-06-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_indicators_consolidate"
down_revision: Union[str, Sequence[str], None] = "0008_bot_users_trade_capital"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------ #
    # 1. INDICATORS: consolida N righe per (market_id, timeframe, timestamp)
    #    in una sola riga con values = jsonb_object_agg(indicator_name, values)
    # ------------------------------------------------------------------ #
    ind_cols = _columns(bind, "indicators")
    if "indicator_name" in ind_cols:
        # Crea tabella temporanea con i dati aggregati
        op.execute("""
            CREATE TEMP TABLE indicators_tmp AS
            SELECT
                MIN(id)                             AS id,
                market_id,
                timeframe,
                timestamp,
                jsonb_object_agg(indicator_name, values) AS values
            FROM indicators
            GROUP BY market_id, timeframe, timestamp
        """)

        # Svuota e ricostruisce la tabella originale senza indicator_name
        op.execute("TRUNCATE indicators")

        # Rimuovi indici che dipendono da indicator_name o che diventeranno
        # ridondanti dopo l'aggiunta del UNIQUE constraint composito
        ind_idxs = _indexes(bind, "indicators")
        for idx in ("ix_indicators_indicator_name", "ix_indicators_timestamp",
                    "ix_indicators_market_id", "ix_indicators_timeframe"):
            if idx in ind_idxs:
                op.drop_index(idx, table_name="indicators")

        op.drop_column("indicators", "indicator_name")

        # Re-inserisce i dati consolidati
        op.execute("""
            INSERT INTO indicators (id, market_id, timeframe, timestamp, values)
            SELECT id, market_id, timeframe, timestamp, values
            FROM indicators_tmp
        """)

        op.execute("DROP TABLE indicators_tmp")

        # Sincronizza la sequenza della PK
        op.execute("""
            SELECT setval(
                pg_get_serial_sequence('indicators', 'id'),
                COALESCE((SELECT MAX(id) FROM indicators), 0)
            )
        """)

        # Aggiunge il vincolo UNIQUE usato dagli upsert
        ind_idxs_after = _indexes(bind, "indicators")
        if "uq_indicators_market_tf_time" not in ind_idxs_after:
            op.create_unique_constraint(
                "uq_indicators_market_tf_time",
                "indicators",
                ["market_id", "timeframe", "timestamp"],
            )

        # Ricrea l'indice composito principale (identico al unique, ma esplicito
        # per compatibilità con query che non usano la constraint direttamente)
        if "ix_indicators_market_tf_time" not in _indexes(bind, "indicators"):
            op.create_index(
                "ix_indicators_market_tf_time",
                "indicators",
                ["market_id", "timeframe", "timestamp"],
            )

    # ------------------------------------------------------------------ #
    # 2. CANDLES: rimuove indici ridondanti
    #
    #   ix_candles_market_tf_time  (212 MB) — identico a uq_candle_market_tf_time
    #   ix_candles_market_id       ( 48 MB) — prefisso coperto dall'unique composito
    #   ix_candles_timeframe       ( 44 MB) — non selettivo da solo
    # ------------------------------------------------------------------ #
    candle_idxs = _indexes(bind, "candles")
    for idx in ("ix_candles_market_tf_time", "ix_candles_market_id", "ix_candles_timeframe"):
        if idx in candle_idxs:
            op.drop_index(idx, table_name="candles")

    # ------------------------------------------------------------------ #
    # 3. Cleanup iniziale: elimina candele e indicatori con storico eccessivo
    #    Le finestre precise saranno mantenute dal DataRetentionService;
    #    qui facciamo solo una pulizia una-tantum del dato pre-Aprile 2026
    #    che non ha più valore operativo (sistema era in fase di avvio).
    # ------------------------------------------------------------------ #
    op.execute("DELETE FROM indicators WHERE timestamp < NOW() - INTERVAL '90 days'")
    op.execute("DELETE FROM candles WHERE open_time < NOW() - INTERVAL '90 days' AND timeframe IN ('5m','15m','30m')")
    op.execute("DELETE FROM candles WHERE open_time < NOW() - INTERVAL '180 days' AND timeframe IN ('1h','4h')")
    op.execute("DELETE FROM candles WHERE open_time < NOW() - INTERVAL '730 days' AND timeframe = '1d'")

    # VACUUM per recuperare lo spazio subito (VACUUM non transazionale, va
    # eseguito fuori transazione; lo facciamo best-effort dopo il commit)


def downgrade() -> None:
    bind = op.get_bind()

    # Ricrea indici candles rimossi
    candle_idxs = _indexes(bind, "candles")
    if "ix_candles_market_tf_time" not in candle_idxs:
        op.create_index(
            "ix_candles_market_tf_time",
            "candles",
            ["market_id", "timeframe", "open_time"],
        )
    if "ix_candles_market_id" not in candle_idxs:
        op.create_index("ix_candles_market_id", "candles", ["market_id"])
    if "ix_candles_timeframe" not in candle_idxs:
        op.create_index("ix_candles_timeframe", "candles", ["timeframe"])

    # Il downgrade completo della consolidazione indicators richiederebbe
    # di esplodere i JSONB aggregati — non implementato, lo schema resta
    # consolidato in caso di rollback.
