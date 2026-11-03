"""persist Hyperliquid websocket L2 snapshots

Revision ID: 0015_market_l2_snapshots
Revises: 0014_leaderboard_copy_paper
Create Date: 2026-07-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_market_l2_snapshots"
down_revision: str | Sequence[str] | None = "0014_leaderboard_copy_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "market_l2_snapshots" not in tables:
        op.create_table(
            "market_l2_snapshots",
            sa.Column("market_id", sa.Integer(), nullable=False),
            sa.Column("bids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("asks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("market_id"),
        )


def downgrade() -> None:
    if "market_l2_snapshots" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("market_l2_snapshots")
