"""position cum_funding_since_open and order fee columns

Revision ID: 0005_position_funding_and_order_fee
Revises: 0004_candle_open_interest
Create Date: 2026-05-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_position_funding_and_order_fee"
down_revision: Union[str, Sequence[str], None] = "0004_candle_open_interest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen alembic_version.version_num if needed: this revision ID is 35 chars, default is varchar(32).
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    # Cumulative funding received/paid since the position opened (positive = received).
    op.add_column("positions", sa.Column("cum_funding_since_open", sa.Numeric(28, 10), nullable=True))
    # Taker/maker fee paid on an order fill (positive = paid, negative = maker rebate).
    op.add_column("orders", sa.Column("fee", sa.Numeric(28, 10), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "cum_funding_since_open")
    op.drop_column("orders", "fee")
