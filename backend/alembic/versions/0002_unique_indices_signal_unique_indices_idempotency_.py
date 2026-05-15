"""signal unique constraint, composite indices, idempotency columns

Revision ID: 0002_unique_indices
Revises: 0001_baseline
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_unique_indices"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column("source_candle_open_time", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("pnl_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("cloid", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint("uq_orders_cloid", "orders", ["cloid"])
    op.create_unique_constraint(
        "uq_signal_market_tf_candle",
        "signals",
        ["market_id", "timeframe", "source_candle_open_time"],
    )
    op.create_index(
        "ix_signals_market_tf_created",
        "signals",
        ["market_id", "timeframe", "created_at"],
    )
    op.create_index(
        "ix_indicators_market_tf_time",
        "indicators",
        ["market_id", "timeframe", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_indicators_market_tf_time", table_name="indicators")
    op.drop_index("ix_signals_market_tf_created", table_name="signals")
    op.drop_constraint("uq_signal_market_tf_candle", "signals", type_="unique")
    op.drop_constraint("uq_orders_cloid", "orders", type_="unique")
    op.drop_column("orders", "cloid")
    op.drop_column("positions", "pnl_updated_at")
    op.drop_column("signals", "source_candle_open_time")
