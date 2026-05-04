"""
backfill_winrate.py — Backfill af won/pnl_usdc for alle copy_orders via CLOB API.

Gamma API kender ikke condition_ids for de 15-minutters/1-times Up-or-Down markeder.
Polymarket CLOB API gør — hvert market response indeholder tokens[] med winner: bool.

Korrekt P&L-logik (size_filled = USDC budget, IKKE shares):
  - Vundet:  shares = size_filled / price
             pnl    = shares * 1.0 - size_filled = size_filled * (1/price - 1)
  - Tabt:    pnl    = -size_filled

Kør EFTER migration 013 (dedup af copy_orders):
    docker compose run --rm executor python -u backfill_winrate.py

Output: printer løbende progress + afsluttende statistik til stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

DB_DSN: str = os.getenv("DB_URL", "postgresql://localhost/polymarket").replace(
    "postgresql+asyncpg://", "postgresql://"
)
CLOB_BASE    = "https://clob.polymarket.com"
REQUEST_DELAY = 0.3   # sekunder mellem requests (CLOB er mere tolerant end Gamma)
MAX_RETRIES   = 3
BATCH_PAUSE_EVERY = 200  # pause hvert N. request
BATCH_PAUSE_SEC   = 10   # sekunder pause


async def _fetch_clob_market(client: httpx.AsyncClient, condition_id: str) -> dict | None:
    """
    Hent market fra CLOB API.

    Endpoint: GET /markets/{condition_id}
    Returnerer dict med 'tokens' array eller None ved fejl/ikke-fundet.
    """
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(f"{CLOB_BASE}/markets/{condition_id}", timeout=10)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"  ⏳ 429 — venter {wait}s (forsøg {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            # Valider at vi fik det rigtige marked
            if data.get("condition_id") == condition_id:
                return data
            return None
        except httpx.TimeoutException:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1)
                continue
            return None
        except httpx.HTTPStatusError:
            return None
    return None


def _get_winner(market: dict) -> str | None:
    """
    Returnerer winning outcome (lowercase) fra CLOB market response.

    CLOB tokens[] har winner: true på det vindende outcome når markedet er resolved.
    Returnerer None hvis ingen token har winner=true (market stadig åben/uafgjort).
    """
    tokens = market.get("tokens") or []
    for token in tokens:
        if token.get("winner") is True:
            return str(token.get("outcome", "")).lower()
    return None


async def main() -> None:
    conn = await asyncpg.connect(dsn=DB_DSN)

    # Hent alle unikke condition_ids med uafklarede orders
    rows = await conn.fetch(
        """
        SELECT DISTINCT condition_id
        FROM copy_orders
        WHERE won IS NULL AND status IN ('paper', 'filled')
        ORDER BY condition_id
        """
    )
    total_markets = len(rows)
    print(f"Fandt {total_markets} markeder at tjekke via CLOB API…")
    print(f"Delay: {REQUEST_DELAY}s per request, pause {BATCH_PAUSE_SEC}s hvert {BATCH_PAUSE_EVERY}. request\n")

    resolved_count  = 0
    updated_orders  = 0
    unresolved_count = 0
    not_found_count = 0
    error_count     = 0

    condition_ids = [r["condition_id"] for r in rows]

    # Print første market som debug
    debug_done = False

    async with httpx.AsyncClient() as client:
        for i, cid in enumerate(condition_ids):
            try:
                market = await _fetch_clob_market(client, cid)

                if market is None:
                    not_found_count += 1
                else:
                    # Debug: print første fundne market
                    if not debug_done:
                        debug_done = True
                        tokens_summary = [
                            {"outcome": t.get("outcome"), "price": t.get("price"), "winner": t.get("winner")}
                            for t in (market.get("tokens") or [])
                        ]
                        print(f"\n🔍 DEBUG — første CLOB-market:")
                        print(f"   question: {market.get('question', '?')}")
                        print(f"   closed:   {market.get('closed')}")
                        print(f"   tokens:   {json.dumps(tokens_summary)}\n")

                    winning_outcome = _get_winner(market)
                    if winning_outcome is None:
                        unresolved_count += 1
                    else:
                        # Opdater copy_orders med korrekt P&L
                        # size_filled = USDC investeret (ikke shares)
                        # won:  pnl = size_filled * (1/price - 1)   [shares * 1.0 - cost]
                        # lost: pnl = -size_filled                   [mister hele investeringen]
                        updated = await conn.fetchval(
                            """
                            WITH upd AS (
                                UPDATE copy_orders
                                SET
                                    won      = (LOWER(outcome) = $2),
                                    pnl_usdc = CASE
                                        WHEN LOWER(outcome) = $2
                                            THEN size_filled * (1.0 / NULLIF(price, 0) - 1.0)
                                        ELSE -size_filled
                                        END
                                WHERE condition_id = $1
                                  AND won IS NULL
                                  AND status IN ('paper', 'filled')
                                RETURNING 1
                            )
                            SELECT COUNT(*) FROM upd
                            """,
                            cid,
                            winning_outcome,
                        )
                        n = int(updated or 0)
                        resolved_count += 1
                        updated_orders += n
                        print(f"  ✅ {cid[:14]}… vinder='{winning_outcome}' → {n} orders opdateret")

            except Exception as exc:
                error_count += 1
                print(f"  ❌ {cid[:14]}… fejl: {exc}")

            # Progress hvert 50. marked
            if (i + 1) % 50 == 0 or (i + 1) == total_markets:
                print(
                    f"  [{i+1:>4}/{total_markets}]  "
                    f"resolved={resolved_count}  "
                    f"unresolved={unresolved_count}  "
                    f"not_found={not_found_count}  "
                    f"fejl={error_count}"
                )

            await asyncio.sleep(REQUEST_DELAY)

            # Pause hvert BATCH_PAUSE_EVERY. request
            if (i + 1) % BATCH_PAUSE_EVERY == 0 and (i + 1) < total_markets:
                print(f"\n  ⏸  Pause {BATCH_PAUSE_SEC}s…\n")
                await asyncio.sleep(BATCH_PAUSE_SEC)

    await conn.close()

    # ── Afsluttende statistik ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"Backfill færdig: {total_markets} markeder tjekket")
    print(f"  Resolved og opdateret:   {resolved_count} markeder ({updated_orders} orders)")
    print(f"  Stadig uafgjort:         {unresolved_count}")
    print(f"  Ikke fundet i CLOB API:  {not_found_count}")
    print(f"  Fejl:                    {error_count}")
    print("=" * 65)

    # ── Aggregeret statistik ─────────────────────────────────────────────────────
    conn2 = await asyncpg.connect(dsn=DB_DSN)
    stats = await conn2.fetchrow(
        """
        SELECT
            COUNT(*)                                            AS total,
            COUNT(*) FILTER (WHERE won = true)                  AS won_count,
            COUNT(*) FILTER (WHERE won = false)                 AS lost_count,
            COUNT(*) FILTER (WHERE won IS NULL)                 AS pending_count,
            COALESCE(SUM(pnl_usdc), 0)                         AS total_pnl,
            COALESCE(SUM(size_filled), 0)                       AS total_invested
        FROM copy_orders
        WHERE status IN ('paper', 'filled')
        """
    )
    by_outcome = await conn2.fetch(
        """
        SELECT
            UPPER(outcome)                                       AS outcome,
            COUNT(*)                                             AS total,
            COUNT(*) FILTER (WHERE won = true)                   AS won_count,
            ROUND(AVG(price)::numeric, 4)                        AS avg_entry_price,
            ROUND(AVG(pnl_usdc)::numeric, 2)                     AS avg_pnl
        FROM copy_orders
        WHERE won IS NOT NULL
        GROUP BY UPPER(outcome)
        ORDER BY total DESC
        """
    )
    daily = await conn2.fetch(
        """
        SELECT
            DATE(timestamp)                                      AS day,
            COUNT(*)                                             AS trades,
            COUNT(*) FILTER (WHERE won = true)                   AS won_count,
            COUNT(*) FILTER (WHERE won = false)                  AS lost_count,
            ROUND(COALESCE(SUM(size_filled), 0)::numeric, 0)     AS invested,
            ROUND(COALESCE(SUM(pnl_usdc), 0)::numeric, 2)        AS pnl
        FROM copy_orders
        WHERE status IN ('paper', 'filled')
        GROUP BY DATE(timestamp)
        ORDER BY day DESC
        LIMIT 7
        """
    )
    await conn2.close()

    total    = int(stats["total"] or 0)
    won      = int(stats["won_count"] or 0)
    lost     = int(stats["lost_count"] or 0)
    pending  = int(stats["pending_count"] or 0)
    total_pnl = float(stats["total_pnl"] or 0)
    invested  = float(stats["total_invested"] or 0)
    resolved_total = won + lost
    win_rate = won / resolved_total if resolved_total > 0 else 0
    roi      = (total_pnl / invested * 100) if invested > 0 else 0

    print(f"\n📊 SAMLET STATISTIK ({total} trades)")
    print(f"   Win rate:        {win_rate:.1%}  ({won}W / {lost}L / {pending} afventer)")
    print(f"   Sim. P&L:        ${total_pnl:+,.2f} USDC")
    print(f"   Sim. investeret: ${invested:,.2f} USDC")
    print(f"   ROI:             {roi:+.2f}%")

    if by_outcome:
        print("\n🎯 WIN RATE PER OUTCOME-TYPE:")
        print(f"   {'Outcome':<8}  {'Win%':>6}  {'W/L':>10}  {'Avg entry':>10}  {'Avg P&L':>10}")
        print("   " + "-" * 54)
        for row in by_outcome:
            ot = int(row["total"])
            ow = int(row["won_count"])
            wr = ow / ot if ot > 0 else 0
            print(
                f"   {row['outcome']:<8}  {wr:>6.1%}  "
                f"{ow:>4}W/{ot-ow:<4}L  "
                f"${float(row['avg_entry_price'] or 0):>8.3f}  "
                f"${float(row['avg_pnl'] or 0):>+8.2f}"
            )

    if daily:
        print("\n📅 DAGLIG TREND (seneste 7 dage):")
        print(f"   {'Dato':<12}  {'Trades':>7}  {'W':>5}  {'L':>5}  {'Win%':>6}  {'Invest':>10}  {'P&L':>12}")
        print("   " + "-" * 70)
        for row in daily:
            ot = int(row["won_count"]) + int(row["lost_count"])
            wr = int(row["won_count"]) / ot if ot > 0 else 0
            print(
                f"   {str(row['day']):<12}  {int(row['trades']):>7}  "
                f"{int(row['won_count']):>5}  {int(row['lost_count']):>5}  "
                f"{wr:>6.1%}  ${float(row['invested']):>8,.0f}  "
                f"${float(row['pnl']):>+10.2f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
