"""add mode column to followed_wallets and copy_orders for dual-track trading

Revision ID: 018
Revises: 017
Create Date: 2026-06-12

mode = 'paper' → simuleret spor (paper bot)
mode = 'live'  → live handelsspor (live bot)
"""
from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # followed_wallets: styrer hvilket spor nye trades fra denne wallet går på
    op.execute("""
        ALTER TABLE followed_wallets
        ADD COLUMN IF NOT EXISTS mode VARCHAR(10) NOT NULL DEFAULT 'paper'
    """)
    op.execute("""
        ALTER TABLE followed_wallets
        ADD CONSTRAINT followed_wallets_mode_check
        CHECK (mode IN ('paper', 'live'))
    """)

    # copy_orders: registrerer hvilket spor ordren blev oprettet på
    op.execute("""
        ALTER TABLE copy_orders
        ADD COLUMN IF NOT EXISTS mode VARCHAR(10) NOT NULL DEFAULT 'paper'
    """)
    op.execute("""
        ALTER TABLE copy_orders
        ADD CONSTRAINT copy_orders_mode_check
        CHECK (mode IN ('paper', 'live'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE copy_orders DROP CONSTRAINT IF EXISTS copy_orders_mode_check")
    op.execute("ALTER TABLE copy_orders DROP COLUMN IF EXISTS mode")
    op.execute("ALTER TABLE followed_wallets DROP CONSTRAINT IF EXISTS followed_wallets_mode_check")
    op.execute("ALTER TABLE followed_wallets DROP COLUMN IF EXISTS mode")
