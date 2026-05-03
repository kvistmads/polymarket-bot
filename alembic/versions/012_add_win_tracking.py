"""add win tracking columns to copy_orders

Revision ID: 012
Revises: 011
Create Date: 2026-05-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # won: NULL = unresolved, TRUE = we copied the winning side, FALSE = we lost
    op.execute("ALTER TABLE copy_orders ADD COLUMN won BOOLEAN")
    op.execute("ALTER TABLE copy_orders ADD COLUMN pnl_usdc NUMERIC(18,4)")
    # Partial index — alleen op resolved orders (NULL rows hebben geen index nodig)
    op.execute("""
        CREATE INDEX ix_copy_orders_won
            ON copy_orders (won)
         WHERE won IS NOT NULL
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_copy_orders_won")
    op.execute("ALTER TABLE copy_orders DROP COLUMN IF EXISTS pnl_usdc")
    op.execute("ALTER TABLE copy_orders DROP COLUMN IF EXISTS won")
