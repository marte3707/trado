"""volume_profile_cache

Revision ID: 0011_volume_profile_cache
Revises: 0010_positions_orders_indices
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_volume_profile_cache"
down_revision: Union[str, Sequence[str], None] = "0010_positions_orders_indices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "volume_profile_cache" not in inspector.get_table_names():
        op.create_table(
            "volume_profile_cache",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("market_id", sa.Integer(), nullable=False),
            sa.Column("timeframe", sa.String(length=8), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("levels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("poc_price", sa.Numeric(28, 10), nullable=True),
            sa.Column("vah_price", sa.Numeric(28, 10), nullable=True),
            sa.Column("val_price", sa.Numeric(28, 10), nullable=True),
            sa.Column("total_volume", sa.Numeric(28, 10), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "market_id",
                "timeframe",
                "window_start",
                "window_end",
                "source",
                name="uq_volume_profile_cache_window",
            ),
        )
        inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("volume_profile_cache")}
    if "ix_volume_profile_cache_market_id" not in indexes:
        op.create_index("ix_volume_profile_cache_market_id", "volume_profile_cache", ["market_id"])
    if "ix_volume_profile_cache_timeframe" not in indexes:
        op.create_index("ix_volume_profile_cache_timeframe", "volume_profile_cache", ["timeframe"])
    if "ix_volume_profile_cache_window_start" not in indexes:
        op.create_index("ix_volume_profile_cache_window_start", "volume_profile_cache", ["window_start"])
    if "ix_volume_profile_cache_window_end" not in indexes:
        op.create_index("ix_volume_profile_cache_window_end", "volume_profile_cache", ["window_end"])
    if "ix_volume_profile_cache_source" not in indexes:
        op.create_index("ix_volume_profile_cache_source", "volume_profile_cache", ["source"])
    if "ix_volume_profile_cache_market_tf_source_end" not in indexes:
        op.create_index(
            "ix_volume_profile_cache_market_tf_source_end",
            "volume_profile_cache",
            ["market_id", "timeframe", "source", "window_end"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "volume_profile_cache" in sa.inspect(bind).get_table_names():
        op.drop_table("volume_profile_cache")
