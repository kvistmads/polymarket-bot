"""create_wallet_score_snapshots

Revision ID: 009
Revises: 008
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE wallet_score_snapshots (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            wallet_id         BIGINT NOT NULL REFERENCES wallets(id),
            snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            trades_total      INTEGER NOT NULL DEFAULT 0,
            trades_won        INTEGER NOT NULL DEFAULT 0,
            win_rate          NUMERIC(6,4),
            sortino_ratio     NUMERIC(8,4),
            max_drawdown      NUMERIC(6,4),
            bull_win_rate     NUMERIC(6,4),
            bear_win_rate     NUMERIC(6,4),
            consistency_score NUMERIC(6,4),
            sizing_entropy    NUMERIC(8,4),
            annual_return_pct NUMERIC(8,4)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_wallet_score_snapshots_wallet_ts
            ON wallet_score_snapshots (wallet_id, snapshot_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallet_score_snapshots CASCADE")
