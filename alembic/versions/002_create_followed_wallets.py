"""create_followed_wallets

Revision ID: 002
Revises: 001
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE followed_wallets (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id         BIGINT NOT NULL REFERENCES wallets(id),
            position_size_pct NUMERIC(4,3),
            followed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            unfollowed_at     TIMESTAMPTZ,
            reason            TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_followed_wallets_active
            ON followed_wallets (wallet_id, unfollowed_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS followed_wallets CASCADE")
