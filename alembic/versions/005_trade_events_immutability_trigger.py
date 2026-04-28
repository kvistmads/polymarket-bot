"""trade_events_immutability_trigger

Revision ID: 005
Revises: 004
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION deny_trade_events_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'trade_events is immutable — UPDATE and DELETE are forbidden';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trade_events_deny_update
            BEFORE UPDATE ON trade_events
            FOR EACH ROW EXECUTE FUNCTION deny_trade_events_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trade_events_deny_delete
            BEFORE DELETE ON trade_events
            FOR EACH ROW EXECUTE FUNCTION deny_trade_events_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trade_events_deny_update ON trade_events")
    op.execute("DROP TRIGGER IF EXISTS trade_events_deny_delete ON trade_events")
    op.execute("DROP FUNCTION IF EXISTS deny_trade_events_mutation")
