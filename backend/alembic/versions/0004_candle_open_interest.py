"""candle open interest column

Revision ID: 0004_candle_open_interest
Revises: 0003_position_stop_updates
Create Date: 2026-05-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_candle_open_interest"
down_revision: Union[str, Sequence[str], None] = "0003_position_stop_updates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("candles", sa.Column("open_interest", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("candles", "open_interest")
