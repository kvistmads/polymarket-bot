"""014_fix_dedup_copy_orders — Korrekt dedup af copy_orders.

Migration 013 virkede ikke: duplikater havde marginalt forskellig timestamp
(ms-niveau fra polling), så GROUP BY med timestamp fjernede intet.

Denne migration:
  1. Dropper det fejlede unique index fra 013
  2. Sletter duplikater baseret på (condition_id, outcome, price, source_wallet_id)
     — ignorerer timestamp, beholder ældste række (lavest id)
  3. Opdaterer won/pnl_usdc på de tilbageværende rækker fra de slettede
     (data bevares — vi kopierer won/pnl fra den slettede til den beholdte
     hvis den beholdte mangler won)
  4. Tilføjer UNIQUE index på trade_event_id — forhindrer executor i at
     oprette to copy_orders for det samme trade_event fremover

Revision ID: 014
Revises: 013
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Trin 1: Drop det fejlede index fra migration 013 ──────────────────────
    op.execute("DROP INDEX IF EXISTS ix_copy_orders_dedup")

    # ── Trin 2: Overfør won/pnl_usdc til den ældste række inden dedup ─────────
    # Hvis den ældste kopi (MIN id) mangler won-status, men en nyere kopi har det,
    # kopieres data over så vi ikke mister backfill-resultater.
    op.execute("""
        UPDATE copy_orders AS target
        SET
            won      = source.won,
            pnl_usdc = source.pnl_usdc
        FROM (
            SELECT DISTINCT ON (condition_id, outcome, ROUND(price::numeric, 4), source_wallet_id)
                condition_id,
                outcome,
                ROUND(price::numeric, 4) AS price_rounded,
                source_wallet_id,
                won,
                pnl_usdc
            FROM copy_orders
            WHERE won IS NOT NULL
            ORDER BY condition_id, outcome, ROUND(price::numeric, 4), source_wallet_id, id ASC
        ) AS source
        WHERE target.condition_id     = source.condition_id
          AND target.outcome          = source.outcome
          AND ROUND(target.price::numeric, 4) = source.price_rounded
          AND target.source_wallet_id = source.source_wallet_id
          AND target.won IS NULL
          AND target.id = (
              SELECT MIN(id) FROM copy_orders c2
              WHERE c2.condition_id      = target.condition_id
                AND c2.outcome           = target.outcome
                AND ROUND(c2.price::numeric, 4) = ROUND(target.price::numeric, 4)
                AND c2.source_wallet_id  = target.source_wallet_id
          )
    """)

    # ── Trin 3: Slet duplikater — behold MIN(id) per unik handel ──────────────
    op.execute("""
        DELETE FROM copy_orders
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM copy_orders
            GROUP BY condition_id, outcome, ROUND(price::numeric, 4), source_wallet_id
        )
    """)

    # ── Trin 4: Unique index på trade_event_id ─────────────────────────────────
    # Forhindrer executor i at oprette duplikater ved fremtidige polls.
    # PARTIAL index — ekskluderer NULL (trade_events uden id, som ikke bør eksistere).
    op.execute("""
        CREATE UNIQUE INDEX ix_copy_orders_trade_event_id
            ON copy_orders (trade_event_id)
         WHERE trade_event_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_copy_orders_trade_event_id")
