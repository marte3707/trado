"""add forward experiment outcomes

Revision ID: 0016_experiment_outcomes
Revises: 0015_market_l2_snapshots
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_experiment_outcomes"
down_revision: str | Sequence[str] | None = "0015_market_l2_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "experiment_outcomes" in tables:
        return
    op.create_table(
        "experiment_outcomes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("experiment_version", sa.String(length=64), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("assignment", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("barrier_label", sa.String(length=32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column("initial_stop_loss", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("first_target_price", sa.Numeric(precision=28, scale=10), nullable=True),
        sa.Column("gross_r", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("estimated_fee_r", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("net_r", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("mae_r", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("mfe_r", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("first_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position_id"),
    )
    op.create_index("ix_experiment_outcomes_position_id", "experiment_outcomes", ["position_id"])
    op.create_index("ix_experiment_outcomes_signal_id", "experiment_outcomes", ["signal_id"])
    op.create_index("ix_experiment_outcomes_market_id", "experiment_outcomes", ["market_id"])
    op.create_index("ix_experiment_outcomes_experiment_version", "experiment_outcomes", ["experiment_version"])
    op.create_index("ix_experiment_outcomes_configuration_hash", "experiment_outcomes", ["configuration_hash"])
    op.create_index("ix_experiment_outcomes_assignment", "experiment_outcomes", ["assignment"])
    op.create_index("ix_experiment_outcomes_timeframe", "experiment_outcomes", ["timeframe"])
    op.create_index("ix_experiment_outcomes_barrier_label", "experiment_outcomes", ["barrier_label"])
    op.create_index("ix_experiment_outcomes_closed_at", "experiment_outcomes", ["closed_at"])
    op.create_index(
        "ix_experiment_outcomes_version_closed",
        "experiment_outcomes",
        ["experiment_version", "closed_at"],
    )


def downgrade() -> None:
    if "experiment_outcomes" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("experiment_outcomes")
