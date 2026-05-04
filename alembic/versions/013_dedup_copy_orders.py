"""013_dedup_copy_orders — Ryd op i duplikater og tilføj unik constraint.

Baggrund:
  monitor.py's deduplication fejlede for trades uden transactionHash.
  Resulterede i op til 59 identiske copy_orders per trade-tick.

Hvad denne migration gør:
  1. Sletter duplikat-rækker fra copy_orders (beholder den rækkke med lavest id)
  2. Tilføjer UNIQUE index på (condition_id, outcome, price, wallet_id, timestamp)
     — forhindrer fremtidige duplikater på DB-niveau

Revision ID: 013
Revises: 012
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Trin 1: Slet duplikat-rækker — behold lavest id per unik kombination ──
    op.execute("""
        DELETE FROM copy_orders
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM copy_orders
            GROUP BY condition_id, outcome, price, wallet_id, timestamp
        )
    """)

    # ── Trin 2: Tilføj unique index så det ikke kan ske igen ──
    # CONCURRENTLY er ikke tilladt inde i en transaktion, men Alembic kører
    # i autocommit-mode ved CREATE INDEX CONCURRENTLY — vi bruger standard her
    # og accepterer kort lock (tabellen er lille efter dedup).
    op.execute("""
        CREATE UNIQUE INDEX ix_copy_orders_dedup
            ON copy_orders (condition_id, outcome, price, wallet_id, timestamp)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_copy_orders_dedup")
