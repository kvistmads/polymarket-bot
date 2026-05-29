"""add partial unique index: one open position per wallet+market+outcome

Revision ID: 017
Revises: 016
Create Date: 2026-05-30

Forhindrer at executor opretter to åbne copy_orders for samme
(source_wallet_id, condition_id, outcome). Partial index på
status IN ('paper','filled') — lukkede positioner tillader re-entry.
"""
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
            ix_copy_orders_active_position
        ON copy_orders (source_wallet_id, condition_id, outcome)
        WHERE status IN ('paper', 'filled')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_copy_orders_active_position")
