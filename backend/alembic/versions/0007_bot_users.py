"""bot_users: onboarding table for Telegram funnel

Revision ID: 0007_bot_users
Revises: 0006_account_snapshot_effective_equity
Create Date: 2026-06-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_bot_users"
down_revision: Union[str, Sequence[str], None] = "0006_account_snapshot_effective_equity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, name: str) -> bool:
    return any(idx["name"] == name for idx in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    # Idempotent: app.main.startup runs Base.metadata.create_all() *before*
    # alembic upgrade, so on existing DBs the table may already exist when this
    # migration runs. On a fresh DB it doesn't, so we still need the DDL here.
    bind = op.get_bind()
    if not _has_table(bind, "bot_users"):
        op.create_table(
            "bot_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
            sa.Column("telegram_username", sa.String(255), nullable=True),
            sa.Column("first_name", sa.String(255), nullable=True),
            sa.Column("language", sa.String(8), nullable=False, server_default="en"),
            sa.Column("source", sa.String(64), nullable=True),
            sa.Column("experience", sa.String(16), nullable=True),
            sa.Column("onboarding_stage", sa.String(32), nullable=False, server_default="started"),
            sa.Column("risk_ack_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("invite_link", sa.Text(), nullable=True),
            sa.Column("joined_channel_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("telegram_user_id", name="uq_bot_users_telegram_user_id"),
        )
    if not _has_index(bind, "bot_users", "ix_bot_users_telegram_user_id"):
        op.create_index("ix_bot_users_telegram_user_id", "bot_users", ["telegram_user_id"])
    if not _has_index(bind, "bot_users", "ix_bot_users_source"):
        op.create_index("ix_bot_users_source", "bot_users", ["source"])
    if not _has_index(bind, "bot_users", "ix_bot_users_onboarding_stage"):
        op.create_index("ix_bot_users_onboarding_stage", "bot_users", ["onboarding_stage"])


def downgrade() -> None:
    op.drop_index("ix_bot_users_onboarding_stage", table_name="bot_users")
    op.drop_index("ix_bot_users_source", table_name="bot_users")
    op.drop_index("ix_bot_users_telegram_user_id", table_name="bot_users")
    op.drop_table("bot_users")
