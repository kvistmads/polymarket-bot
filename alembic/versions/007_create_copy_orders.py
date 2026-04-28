"""create_copy_orders

Revision ID: 007
Revises: 006
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE copy_orders (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_wallet_id BIGINT NOT NULL REFERENCES wallets(id),
            trade_event_id   BIGINT REFERENCES trade_events(id),
            condition_id     TEXT NOT NULL,
            outcome          TEXT NOT NULL,
            side             TEXT NOT NULL,
            size_requested   NUMERIC(18,4) NOT NULL,
            size_filled      NUMERIC(18,4),
            price            NUMERIC(10,6),
            status           TEXT NOT NULL DEFAULT 'pending',
            error_msg        TEXT,
            timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT copy_orders_side_check
                CHECK (side IN ('buy', 'sell')),
            CONSTRAINT copy_orders_status_check
                CHECK (status IN (
                    'pending', 'submitted', 'filled',
                    'failed', 'cancelled', 'paper'
                ))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_copy_orders_wallet_ts
            ON copy_orders (source_wallet_id, timestamp)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_copy_orders_status_ts
            ON copy_orders (status, timestamp)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS copy_orders CASCADE")
