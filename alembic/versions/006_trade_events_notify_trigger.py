"""trade_events_notify_trigger

Revision ID: 006
Revises: 005
Create Date: 2026-04-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_new_trade_event()
        RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('new_trade', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trade_events_notify
            AFTER INSERT ON trade_events
            FOR EACH ROW EXECUTE FUNCTION notify_new_trade_event()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trade_events_notify ON trade_events")
    op.execute("DROP FUNCTION IF EXISTS notify_new_trade_event")
