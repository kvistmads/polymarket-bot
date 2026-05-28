"""add unique index on copy_orders.trade_event_id

Revision ID: 016
Revises: 015
Create Date: 2026-05-28

Tilføjer UNIQUE index på copy_orders.trade_event_id.
Krævet af ON CONFLICT (trade_event_id) DO NOTHING i log_copy_order().

Partial index (WHERE trade_event_id IS NOT NULL) sikrer at gamle copy_orders
uden trade_event_id (de 30.609 paper-orders fra gammel wallet) ikke er i konflikt.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS copy_orders_trade_event_id_uniq
        ON copy_orders (trade_event_id)
        WHERE trade_event_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS copy_orders_trade_event_id_uniq")
