"""create_wallet_scores

Revision ID: 008
Revises: 007
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE wallet_scores (
            wallet_id          BIGINT PRIMARY KEY REFERENCES wallets(id),
            trades_total       INTEGER NOT NULL DEFAULT 0,
            trades_won         INTEGER NOT NULL DEFAULT 0,
            win_rate           NUMERIC(6,4),
            sortino_ratio      NUMERIC(8,4),
            max_drawdown       NUMERIC(6,4),
            bull_win_rate      NUMERIC(6,4),
            bear_win_rate      NUMERIC(6,4),
            consistency_score  NUMERIC(6,4),
            sizing_entropy     NUMERIC(8,4),
            estimated_bankroll NUMERIC(18,2),
            annual_return_pct  NUMERIC(8,4),
            last_scored_at     TIMESTAMPTZ
        )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallet_scores CASCADE")
