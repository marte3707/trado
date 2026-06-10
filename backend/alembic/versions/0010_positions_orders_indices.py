"""positions/orders: indici composti per dashboard e validazione live

Aggiunge:
  * ix_positions_status_mode (positions.status, positions.mode) — usato dal count
    posizioni live aperte nell'OrderValidator e dai filtri stato/modalità della UI.
  * ix_orders_position_status (orders.position_id, orders.status) — join orders per
    posizione filtrati per stato (reconciliation, dashboard).

Revision ID: 0010_positions_orders_indices
Revises: 0009_indicators_consolidate
Create Date: 2026-06-09
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0010_positions_orders_indices"
down_revision: Union[str, Sequence[str], None] = "0009_indicators_consolidate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "ix_positions_status_mode" not in _indexes(bind, "positions"):
        op.create_index("ix_positions_status_mode", "positions", ["status", "mode"])
    if "ix_orders_position_status" not in _indexes(bind, "orders"):
        op.create_index("ix_orders_position_status", "orders", ["position_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_positions_status_mode" in _indexes(bind, "positions"):
        op.drop_index("ix_positions_status_mode", table_name="positions")
    if "ix_orders_position_status" in _indexes(bind, "orders"):
        op.drop_index("ix_orders_position_status", table_name="orders")
