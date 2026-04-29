"""create_positions

Revision ID: 003
Revises: 002
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE positions (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id       BIGINT NOT NULL REFERENCES wallets(id),
            condition_id    TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            size            NUMERIC(18,4) NOT NULL DEFAULT 0,
            avg_price       NUMERIC(10,6),
            cur_price       NUMERIC(10,6),
            current_value   NUMERIC(18,4),
            cash_pnl        NUMERIC(18,4),
            percent_pnl     NUMERIC(10,4),
            token_id        TEXT,
            title           TEXT,
            first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            status          TEXT NOT NULL DEFAULT 'open',
            CONSTRAINT positions_status_check
                CHECK (status IN ('open', 'closed')),
            CONSTRAINT positions_unique
                UNIQUE (wallet_id, condition_id, outcome)
        )
        """)
    op.execute("""
        CREATE INDEX idx_positions_wallet_status
            ON positions (wallet_id, status)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS positions CASCADE")
