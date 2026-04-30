"""
filter_db.py — Database-helpers til filter.py.

Eksponerer: upsert_wallet, save_scores, get_wallet_label, get_active_wallets.
Skrives KUN til: wallets, followed_wallets, wallet_scores, wallet_score_snapshots.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from db import acquire
from filter_scores import WalletScores

log = logging.getLogger(__name__)


async def upsert_wallet(address: str, label: str | None) -> int:
    """INSERT wallet eller UPDATE label. Returnér wallet_id."""
    async with acquire() as conn:
        if label:
            row = await conn.fetchrow(
                """
                INSERT INTO wallets (address, label)
                VALUES ($1, $2)
                ON CONFLICT (address) DO UPDATE SET label = EXCLUDED.label
                RETURNING id
                """,
                address,
                label,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO wallets (address)
                VALUES ($1)
                ON CONFLICT (address) DO NOTHING
                RETURNING id
                """,
                address,
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT id FROM wallets WHERE address = $1", address
                )
        return row["id"]  # type: ignore[index]


async def save_scores(wallet_id: int, scores: WalletScores) -> None:
    """Gem scores i wallet_scores (upsert) + insert snapshot (atomisk transaktion)."""
    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO wallet_scores (
                    wallet_id, trades_total, trades_won, win_rate,
                    sortino_ratio, max_drawdown, bull_win_rate, bear_win_rate,
                    consistency_score, sizing_entropy, estimated_bankroll,
                    annual_return_pct, last_scored_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,now())
                ON CONFLICT (wallet_id) DO UPDATE SET
                    trades_total       = EXCLUDED.trades_total,
                    trades_won         = EXCLUDED.trades_won,
                    win_rate           = EXCLUDED.win_rate,
                    sortino_ratio      = EXCLUDED.sortino_ratio,
                    max_drawdown       = EXCLUDED.max_drawdown,
                    bull_win_rate      = EXCLUDED.bull_win_rate,
                    bear_win_rate      = EXCLUDED.bear_win_rate,
                    consistency_score  = EXCLUDED.consistency_score,
                    sizing_entropy     = EXCLUDED.sizing_entropy,
                    estimated_bankroll = EXCLUDED.estimated_bankroll,
                    annual_return_pct  = EXCLUDED.annual_return_pct,
                    last_scored_at     = now()
                """,
                wallet_id,
                scores.trades_total,
                scores.trades_won,
                scores.win_rate,
                scores.sortino_ratio,
                scores.max_drawdown,
                scores.bull_win_rate,
                scores.bear_win_rate,
                scores.consistency_score,
                scores.sizing_entropy,
                scores.estimated_bankroll,
                scores.annual_return_pct,
            )
            await conn.execute(
                """
                INSERT INTO wallet_score_snapshots (
                    wallet_id, trades_total, trades_won, win_rate,
                    sortino_ratio, max_drawdown, bull_win_rate, bear_win_rate,
                    consistency_score, sizing_entropy, annual_return_pct
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                wallet_id,
                scores.trades_total,
                scores.trades_won,
                scores.win_rate,
                scores.sortino_ratio,
                scores.max_drawdown,
                scores.bull_win_rate,
                scores.bear_win_rate,
                scores.consistency_score,
                scores.sizing_entropy,
                scores.annual_return_pct,
            )
    log.debug("Scores gemt for wallet_id=%d", wallet_id)


async def get_wallet_label(address: str) -> str | None:
    """Hent label for en wallet-adresse. Returnér None hvis ikke sat."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT label FROM wallets WHERE address = $1", address.lower()
        )
        return row["label"] if row else None


async def get_active_wallets() -> list[dict]:
    """Hent alle aktive (unfollowed_at IS NULL) fulgte wallets fra DB."""
    async with acquire() as conn:
        rows = await conn.fetch("""
            SELECT w.id, w.address, w.label
            FROM followed_wallets fw
            JOIN wallets w ON w.id = fw.wallet_id
            WHERE fw.unfollowed_at IS NULL
            """)
        return [dict(r) for r in rows]


async def get_followed_wallets_with_scores(
    min_sortino: float | None = None,
) -> list[dict]:
    """Hent aktive wallets med seneste scores til list-kommandoen."""
    async with acquire() as conn:
        rows = await conn.fetch("""
            SELECT w.address, w.label, ws.win_rate, ws.sortino_ratio,
                   ws.max_drawdown, ws.trades_total, ws.last_scored_at,
                   fw.position_size_pct, fw.followed_at
            FROM followed_wallets fw
            JOIN wallets w ON w.id = fw.wallet_id
            LEFT JOIN wallet_scores ws ON ws.wallet_id = fw.wallet_id
            WHERE fw.unfollowed_at IS NULL
            ORDER BY ws.sortino_ratio DESC NULLS LAST
            """)
    result = [dict(r) for r in rows]
    if min_sortino is not None:
        result = [
            r
            for r in result
            if r["sortino_ratio"] is not None
            and float(r["sortino_ratio"]) >= min_sortino
        ]
    return result


async def follow_wallet(wallet_id: int, size_pct: float) -> None:
    """Insert ny række i followed_wallets."""
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO followed_wallets (wallet_id, position_size_pct)
            VALUES ($1, $2)
            """,
            wallet_id,
            Decimal(str(size_pct)),
        )


async def get_active_follow(wallet_id: int) -> dict | None:
    """Returnér eksisterende aktiv follow-række eller None."""
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM followed_wallets
            WHERE wallet_id = $1 AND unfollowed_at IS NULL
            """,
            wallet_id,
        )
        return dict(row) if row else None


async def unfollow_wallet(address: str, reason: str | None) -> int:
    """Sæt unfollowed_at på aktiv follow. Returnér antal påvirkede rækker."""
    async with acquire() as conn:
        result = await conn.execute(
            """
            UPDATE followed_wallets
            SET unfollowed_at = now(), reason = $2
            WHERE wallet_id = (SELECT id FROM wallets WHERE address = $1)
              AND unfollowed_at IS NULL
            """,
            address,
            reason,
        )
    return int(result.split()[-1])
