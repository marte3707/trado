"""bot_users: rename experience -> trade_capital

The semantic of the second onboarding question changed from self-declared
trading experience (beginner/intermediate/expert) to declared crypto trading
capital range (under_1k / 1k_to_10k / over_10k, or NULL when skipped). Old
column values are wiped during rename because they no longer fit the new
domain.

Revision ID: 0008_bot_users_trade_capital
Revises: 0007_bot_users
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_bot_users_trade_capital"
down_revision: Union[str, Sequence[str], None] = "0007_bot_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "bot_users")
    # Idempotent: on a fresh DB Base.metadata.create_all() already created the
    # column with the new name, so the rename has nothing to do.
    if "experience" in cols and "trade_capital" not in cols:
        op.alter_column("bot_users", "experience", new_column_name="trade_capital")
        # Old experience labels (beginner/intermediate/expert) don't map onto
        # the new capital ranges — clear them so we don't keep stale data.
        op.execute("UPDATE bot_users SET trade_capital = NULL")


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind, "bot_users")
    if "trade_capital" in cols and "experience" not in cols:
        op.alter_column("bot_users", "trade_capital", new_column_name="experience")
        op.execute("UPDATE bot_users SET experience = NULL")
