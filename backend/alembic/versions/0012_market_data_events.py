"""market data websocket close-event queue

Revision ID: 0012_market_data_events
Revises: 0011_volume_profile_cache
Create Date: 2026-06-19
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_market_data_events"
down_revision: str | Sequence[str] | None = "0011_volume_profile_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "market_data_events" not in inspector.get_table_names():
        op.create_table(
            "market_data_events",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("market_id", sa.Integer(), nullable=False),
            sa.Column("timeframe", sa.String(length=8), nullable=False),
            sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "market_id",
                "timeframe",
                "candle_open_time",
                name="uq_market_data_event_candle",
            ),
        )
        inspector = sa.inspect(bind)

    indexes = {index["name"] for index in inspector.get_indexes("market_data_events")}
    if "ix_market_data_events_market_id" not in indexes:
        op.create_index(
            "ix_market_data_events_market_id",
            "market_data_events",
            ["market_id"],
        )
    if "ix_market_data_events_timeframe" not in indexes:
        op.create_index(
            "ix_market_data_events_timeframe",
            "market_data_events",
            ["timeframe"],
        )
    if "ix_market_data_events_candle_open_time" not in indexes:
        op.create_index(
            "ix_market_data_events_candle_open_time",
            "market_data_events",
            ["candle_open_time"],
        )
    if "ix_market_data_events_pending" not in indexes:
        op.create_index(
            "ix_market_data_events_pending",
            "market_data_events",
            ["processed_at", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "market_data_events" in sa.inspect(bind).get_table_names():
        op.drop_table("market_data_events")
