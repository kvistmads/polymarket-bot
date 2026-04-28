"""create_wallets

Revision ID: 001
Revises:
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE wallets (
            id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            address  TEXT NOT NULL,
            label    TEXT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            notes    TEXT,
            CONSTRAINT wallets_address_key UNIQUE (address)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_wallets_address ON wallets (address)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallets CASCADE")
