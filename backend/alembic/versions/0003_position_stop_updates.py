"""position stop update history

Revision ID: 0003_position_stop_updates
Revises: 0002_unique_indices
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_position_stop_updates"
down_revision: Union[str, Sequence[str], None] = "0002_unique_indices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_stop_updates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position_id", sa.Integer(), nullable=False),
        sa.Column("old_stop_loss", sa.Numeric(28, 10), nullable=True),
        sa.Column("new_stop_loss", sa.Numeric(28, 10), nullable=False),
        sa.Column("current_price", sa.Numeric(28, 10), nullable=False),
        sa.Column("r_multiple", sa.Numeric(18, 8), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_position_stop_updates_position_id"),
        "position_stop_updates",
        ["position_id"],
        unique=False,
    )
    op.create_index(
        "ix_position_stop_updates_position_created",
        "position_stop_updates",
        ["position_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_position_stop_updates_reason"),
        "position_stop_updates",
        ["reason"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_position_stop_updates_reason"), table_name="position_stop_updates")
    op.drop_index(
        "ix_position_stop_updates_position_created", table_name="position_stop_updates"
    )
    op.drop_index(
        op.f("ix_position_stop_updates_position_id"), table_name="position_stop_updates"
    )
    op.drop_table("position_stop_updates")
