"""create_market_metadata

Revision ID: 010
Revises: 009
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE market_metadata (
            condition_id   TEXT PRIMARY KEY,
            title          TEXT,
            slug           TEXT,
            outcomes       JSONB,
            clob_token_ids JSONB,
            fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market_metadata CASCADE")
