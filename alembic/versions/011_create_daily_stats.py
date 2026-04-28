"""create_daily_stats

Revision ID: 011
Revises: 010
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE daily_stats (
            date               DATE PRIMARY KEY DEFAULT CURRENT_DATE,
            total_spent        NUMERIC(18,4) NOT NULL DEFAULT 0,
            total_returned     NUMERIC(18,4) NOT NULL DEFAULT 0,
            realized_pnl       NUMERIC(18,4)
                               GENERATED ALWAYS AS (total_returned - total_spent)
                               STORED,
            orders_count       INTEGER NOT NULL DEFAULT 0,
            paper_orders_count INTEGER NOT NULL DEFAULT 0,
            last_updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_stats CASCADE")
