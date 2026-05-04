"""013_dedup_copy_orders — Placeholder (det egentlige dedup sker i migration 014).

Migration 013 forsøgte dedup med forkert kolonnenavn (wallet_id → source_wallet_id)
og med timestamp i GROUP BY (virker ikke da duplikater har ms-forskellig timestamp).
Migration 014 laver den korrekte dedup.

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
    # Ingen ændringer — migration 014 håndterer det korrekte dedup
    pass


def downgrade() -> None:
    pass
