"""account_snapshots: add effective_account_value and free_spot_usdc columns

Revision ID: 0006_account_snapshot_effective_equity
Revises: 0005_position_funding_and_order_fee
Create Date: 2026-05-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_account_snapshot_effective_equity"
down_revision: Union[str, Sequence[str], None] = "0005_position_funding_and_order_fee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("account_snapshots", sa.Column("effective_account_value", sa.Numeric(28, 10), nullable=True))
    op.add_column("account_snapshots", sa.Column("free_spot_usdc", sa.Numeric(28, 10), nullable=True))


def downgrade() -> None:
    op.drop_column("account_snapshots", "free_spot_usdc")
    op.drop_column("account_snapshots", "effective_account_value")
