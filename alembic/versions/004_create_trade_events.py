"""create_trade_events

Revision ID: 004
Revises: 003
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE trade_events (
            id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id      BIGINT NOT NULL REFERENCES wallets(id),
            condition_id   TEXT NOT NULL,
            outcome        TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            old_size       NUMERIC(18,4),
            new_size       NUMERIC(18,4) NOT NULL,
            price_at_event NUMERIC(10,6),
            pnl_at_close   NUMERIC(18,4),
            timestamp      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT trade_events_event_type_check
                CHECK (event_type IN ('opened', 'closed', 'resized'))
        )
        """)
    op.execute("""
        CREATE INDEX idx_trade_events_wallet_ts
            ON trade_events (wallet_id, timestamp)
        """)
    op.execute("""
        CREATE INDEX idx_trade_events_condition_ts
            ON trade_events (condition_id, timestamp)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trade_events CASCADE")
