"""leaderboard copy paper positions

Revision ID: 0014_leaderboard_copy_paper
Revises: 0013_leaderboard_alpha
Create Date: 2026-07-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_leaderboard_copy_paper"
down_revision: str | Sequence[str] | None = "0013_leaderboard_alpha"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    if "leaderboard_copy_paper_positions" not in tables:
        op.create_table(
            "leaderboard_copy_paper_positions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trader_id", sa.Integer(), nullable=False),
            sa.Column("open_fill_id", sa.Integer(), nullable=False),
            sa.Column("close_fill_id", sa.Integer(), nullable=True),
            sa.Column("coin", sa.String(length=64), nullable=False),
            sa.Column("direction", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
            sa.Column("source_open_order_id", sa.String(length=128), nullable=True),
            sa.Column("source_close_order_id", sa.String(length=128), nullable=True),
            sa.Column("entry_price", sa.Numeric(28, 10), nullable=False),
            sa.Column("exit_price", sa.Numeric(28, 10), nullable=True),
            sa.Column("copy_size", sa.Numeric(28, 10), nullable=False),
            sa.Column("copy_notional_usd", sa.Numeric(28, 10), nullable=False),
            sa.Column("original_notional_usd", sa.Numeric(28, 10), nullable=True),
            sa.Column("entry_fee_usdc", sa.Numeric(28, 10), server_default="0", nullable=False),
            sa.Column("exit_fee_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("gross_pnl_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("net_pnl_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("pnl_pct", sa.Numeric(18, 8), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("close_reason", sa.String(length=64), nullable=True),
            sa.Column("metadata", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["trader_id"], ["leaderboard_traders.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["open_fill_id"], ["leaderboard_trader_fills.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["close_fill_id"], ["leaderboard_trader_fills.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("open_fill_id"),
        )

    indexes = _index_names("leaderboard_copy_paper_positions")
    index_specs = {
        "ix_leaderboard_copy_paper_positions_trader_id": ["trader_id"],
        "ix_leaderboard_copy_paper_positions_open_fill_id": ["open_fill_id"],
        "ix_leaderboard_copy_paper_positions_close_fill_id": ["close_fill_id"],
        "ix_leaderboard_copy_paper_positions_coin": ["coin"],
        "ix_leaderboard_copy_paper_positions_direction": ["direction"],
        "ix_leaderboard_copy_paper_positions_status": ["status"],
        "ix_leaderboard_copy_paper_positions_source_open_order_id": ["source_open_order_id"],
        "ix_leaderboard_copy_paper_positions_source_close_order_id": ["source_close_order_id"],
        "ix_leaderboard_copy_paper_positions_opened_at": ["opened_at"],
        "ix_leaderboard_copy_paper_positions_closed_at": ["closed_at"],
        "ix_leaderboard_copy_trader_status": ["trader_id", "status"],
        "ix_leaderboard_copy_coin_status": ["coin", "status"],
        "ix_leaderboard_copy_closed_at": ["closed_at"],
    }
    for name, columns in index_specs.items():
        if name not in indexes:
            op.create_index(name, "leaderboard_copy_paper_positions", columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "leaderboard_copy_paper_positions" in tables:
        op.drop_table("leaderboard_copy_paper_positions")
