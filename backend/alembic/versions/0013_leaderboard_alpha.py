"""leaderboard alpha watchlist

Revision ID: 0013_leaderboard_alpha
Revises: 0012_market_data_events
Create Date: 2026-07-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_leaderboard_alpha"
down_revision: str | Sequence[str] | None = "0012_market_data_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    if "leaderboard_traders" not in tables:
        op.create_table(
            "leaderboard_traders",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("address", sa.String(length=42), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=True),
            sa.Column(
                "source",
                sa.String(length=64),
                server_default="hyperliquid_leaderboard",
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_status", sa.String(length=32), nullable=True),
            sa.Column("last_sync_error", sa.Text(), nullable=True),
            sa.Column("last_seen_fill_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("address"),
        )

    if "leaderboard_trader_fills" not in tables:
        op.create_table(
            "leaderboard_trader_fills",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trader_id", sa.Integer(), nullable=False),
            sa.Column("fill_key", sa.String(length=128), nullable=False),
            sa.Column("coin", sa.String(length=64), nullable=False),
            sa.Column("side", sa.String(length=16), nullable=True),
            sa.Column("direction", sa.String(length=16), nullable=True),
            sa.Column("intent", sa.String(length=16), nullable=True),
            sa.Column("price", sa.Numeric(28, 10), nullable=True),
            sa.Column("size", sa.Numeric(28, 10), nullable=True),
            sa.Column("notional_usd", sa.Numeric(28, 10), nullable=True),
            sa.Column("closed_pnl", sa.Numeric(28, 10), nullable=True),
            sa.Column("fee", sa.Numeric(28, 10), nullable=True),
            sa.Column("crossed", sa.Boolean(), nullable=True),
            sa.Column("oid", sa.String(length=128), nullable=True),
            sa.Column("raw_data", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["trader_id"], ["leaderboard_traders.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("trader_id", "fill_key", name="uq_leaderboard_trader_fill"),
        )

    if "leaderboard_trader_snapshots" not in tables:
        op.create_table(
            "leaderboard_trader_snapshots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trader_id", sa.Integer(), nullable=False),
            sa.Column("lookback_days", sa.Integer(), server_default="30", nullable=False),
            sa.Column("fills_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("closing_fills_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("open_fills_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("net_pnl_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("closed_pnl_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("fees_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("volume_usdc", sa.Numeric(28, 10), nullable=True),
            sa.Column("win_rate", sa.Numeric(12, 6), nullable=True),
            sa.Column("profit_factor", sa.Numeric(18, 6), nullable=True),
            sa.Column("quality_score", sa.Integer(), server_default="0", nullable=False),
            sa.Column("classification", sa.String(length=32), server_default="insufficient", nullable=False),
            sa.Column("metrics", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["trader_id"], ["leaderboard_traders.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    indexes = _index_names("leaderboard_traders")
    if "ix_leaderboard_traders_address" not in indexes:
        op.create_index("ix_leaderboard_traders_address", "leaderboard_traders", ["address"])
    if "ix_leaderboard_traders_source" not in indexes:
        op.create_index("ix_leaderboard_traders_source", "leaderboard_traders", ["source"])
    if "ix_leaderboard_traders_enabled" not in indexes:
        op.create_index("ix_leaderboard_traders_enabled", "leaderboard_traders", ["enabled"])

    indexes = _index_names("leaderboard_trader_fills")
    if "ix_leaderboard_trader_fills_trader_id" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_trader_id", "leaderboard_trader_fills", ["trader_id"])
    if "ix_leaderboard_trader_fills_coin" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_coin", "leaderboard_trader_fills", ["coin"])
    if "ix_leaderboard_trader_fills_side" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_side", "leaderboard_trader_fills", ["side"])
    if "ix_leaderboard_trader_fills_direction" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_direction", "leaderboard_trader_fills", ["direction"])
    if "ix_leaderboard_trader_fills_intent" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_intent", "leaderboard_trader_fills", ["intent"])
    if "ix_leaderboard_trader_fills_filled_at" not in indexes:
        op.create_index("ix_leaderboard_trader_fills_filled_at", "leaderboard_trader_fills", ["filled_at"])
    if "ix_leaderboard_fills_trader_time" not in indexes:
        op.create_index(
            "ix_leaderboard_fills_trader_time",
            "leaderboard_trader_fills",
            ["trader_id", "filled_at"],
        )
    if "ix_leaderboard_fills_coin_direction_time" not in indexes:
        op.create_index(
            "ix_leaderboard_fills_coin_direction_time",
            "leaderboard_trader_fills",
            ["coin", "direction", "filled_at"],
        )

    indexes = _index_names("leaderboard_trader_snapshots")
    if "ix_leaderboard_trader_snapshots_trader_id" not in indexes:
        op.create_index("ix_leaderboard_trader_snapshots_trader_id", "leaderboard_trader_snapshots", ["trader_id"])
    if "ix_leaderboard_trader_snapshots_lookback_days" not in indexes:
        op.create_index(
            "ix_leaderboard_trader_snapshots_lookback_days",
            "leaderboard_trader_snapshots",
            ["lookback_days"],
        )
    if "ix_leaderboard_trader_snapshots_quality_score" not in indexes:
        op.create_index(
            "ix_leaderboard_trader_snapshots_quality_score",
            "leaderboard_trader_snapshots",
            ["quality_score"],
        )
    if "ix_leaderboard_trader_snapshots_classification" not in indexes:
        op.create_index(
            "ix_leaderboard_trader_snapshots_classification",
            "leaderboard_trader_snapshots",
            ["classification"],
        )
    if "ix_leaderboard_trader_snapshots_created_at" not in indexes:
        op.create_index(
            "ix_leaderboard_trader_snapshots_created_at",
            "leaderboard_trader_snapshots",
            ["created_at"],
        )
    if "ix_leaderboard_snapshots_trader_created" not in indexes:
        op.create_index(
            "ix_leaderboard_snapshots_trader_created",
            "leaderboard_trader_snapshots",
            ["trader_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "leaderboard_trader_snapshots" in tables:
        op.drop_table("leaderboard_trader_snapshots")
    if "leaderboard_trader_fills" in tables:
        op.drop_table("leaderboard_trader_fills")
    if "leaderboard_traders" in tables:
        op.drop_table("leaderboard_traders")
