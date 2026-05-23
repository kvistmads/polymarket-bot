"""add sell tracking to copy_orders

Revision ID: 015
Revises: 014
Create Date: 2026-05-23

Tilføjer:
  sell_price      — pris hvortil vi solgte (kopieret fra fulgt wallet)
  sell_timestamp  — hvornår salget skete
  'sold'          — nyt status-værd: position lukket tidligt via sell-kopiering
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE copy_orders ADD COLUMN sell_price NUMERIC(10,6)")
    op.execute("ALTER TABLE copy_orders ADD COLUMN sell_timestamp TIMESTAMPTZ")

    # Udvid status-constraint til at inkludere 'sold'
    op.execute("ALTER TABLE copy_orders DROP CONSTRAINT copy_orders_status_check")
    op.execute("""
        ALTER TABLE copy_orders ADD CONSTRAINT copy_orders_status_check
        CHECK (status IN (
            'pending', 'submitted', 'filled',
            'failed', 'cancelled', 'paper', 'sold'
        ))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE copy_orders DROP CONSTRAINT copy_orders_status_check")
    op.execute("""
        ALTER TABLE copy_orders ADD CONSTRAINT copy_orders_status_check
        CHECK (status IN (
            'pending', 'submitted', 'filled',
            'failed', 'cancelled', 'paper'
        ))
    """)
    op.execute("ALTER TABLE copy_orders DROP COLUMN IF EXISTS sell_timestamp")
    op.execute("ALTER TABLE copy_orders DROP COLUMN IF EXISTS sell_price")
