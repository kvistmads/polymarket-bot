"""
backfill_winrate.py — Éngangs-backfill af won/pnl_usdc for alle copy_orders.

Henter resolution-status fra Gamma API for alle condition_ids i copy_orders
og opdaterer won + pnl_usdc kolonner. Kør én gang efter migration 012.

Brug:
    docker compose run --rm executor python backfill_winrate.py

Output: printer løbende progress + afsluttende statistik til stdout.
"""

from __future__ import annotations

import asyncio
import json
import os

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

DB_DSN: str = os.getenv("DB_URL", "postgresql://localhost/polymarket").replace(
    "postgresql+asyncpg://", "postgresql://"
)
GAMMA_BASE = "https://gamma-api.polymarket.com"
REQUEST_DELAY = 1.5   # sekunder mellem requests for at undgå 429
MAX_RETRIES = 3       # antal genforsøg ved 429

_debug_printed = False  # print første API-response én gang til debug


async def _fetch_market(client: httpx.AsyncClient, condition_id: str) -> dict | None:
    """Hent market fra Gamma API med retry ved 429. Returnerer første market eller None."""
    global _debug_printed
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(
                f"{GAMMA_BASE}/markets",
                params={"condition_id": condition_id},
            )
            if r.status_code == 429:
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"  ⏳ 429 rate limit — venter {wait}s (forsøg {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1)
                continue
            raise e

        markets = data if isinstance(data, list) else [data]
        if not markets:
            return None

        # Print første response én gang så vi kan se feltnavnene
        if not _debug_printed:
            _debug_printed = True
            sample = {k: v for k, v in markets[0].items()
                      if k in ("resolved", "closed", "active", "outcomes",
                               "outcomePrices", "resolvedBy", "resolutionTime",
                               "endDateIso", "question")}
            print(f"\n🔍 DEBUG — første API-response (nøglefelter):\n"
                  f"  {json.dumps(sample, indent=2)}\n")

        return markets[0]

    return None  # alle forsøg fejlede


async def _is_resolved(market: dict) -> str | None:
    """
    Returnerer winning outcome (lowercase) hvis markedet er afgjort, ellers None.

    Gamma API bruger enten:
      - market["resolved"] == True  + outcomePrices  (primær)
      - market["closed"] == True + outcomePrices ≈ 1.0  (fallback)
    """
    outcomes: list = market.get("outcomes") or []
    outcome_prices: list = market.get("outcomePrices") or []

    # Forsøg 1: eksplicit resolved-flag
    if market.get("resolved"):
        for i, price_str in enumerate(outcome_prices):
            try:
                if float(price_str) >= 0.99 and i < len(outcomes):
                    return str(outcomes[i]).lower()
            except (ValueError, TypeError):
                continue

    # Forsøg 2: closed market med klar vinder i outcomePrices (settled men felt mangler)
    if market.get("closed") or not market.get("active", True):
        for i, price_str in enumerate(outcome_prices):
            try:
                if float(price_str) >= 0.99 and i < len(outcomes):
                    return str(outcomes[i]).lower()
            except (ValueError, TypeError):
                continue

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
    print(f"Fandt {total_markets} markeder at tjekke…")
    print(f"Bruger sekventielle requests med {REQUEST_DELAY}s delay for at undgå rate limit.\n")

    resolved_count = 0
    updated_orders = 0
    unresolved_count = 0
    error_count = 0

    condition_ids = [r["condition_id"] for r in rows]

    async with httpx.AsyncClient(timeout=15) as client:
        for i, cid in enumerate(condition_ids):
            try:
                market = await _fetch_market(client, cid)
                if market is None:
                    unresolved_count += 1
                else:
                    winning_outcome = await _is_resolved(market)
                    if winning_outcome is None:
                        unresolved_count += 1
                    else:
                        updated = await conn.fetchval(
                            """
                            WITH upd AS (
                                UPDATE copy_orders
                                SET
                                    won      = (LOWER(outcome) = $2),
                                    pnl_usdc = CASE
                                        WHEN LOWER(outcome) = $2
                                            THEN size_filled * (1 - price)
                                        ELSE -(size_filled * price)
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
                    f"  [{i+1:>4}/{total_markets}] "
                    f"resolved={resolved_count}  "
                    f"unresolved={unresolved_count}  "
                    f"fejl={error_count}"
                )

            await asyncio.sleep(REQUEST_DELAY)

            # Pause hvert 100. request for at undgå rate limit
            if (i + 1) % 100 == 0 and (i + 1) < total_markets:
                print(f"  ⏸  Pause 15s for at undgå rate limit…")
                await asyncio.sleep(15)

    await conn.close()

    # ── Afsluttende statistik ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"Backfill færdig: {total_markets} markeder tjekket")
    print(f"  Resolved og opdateret: {resolved_count} markeder ({updated_orders} orders)")
    print(f"  Endnu ikke resolved:   {unresolved_count}")
    print(f"  Fejl:                  {error_count}")
    print("=" * 60)

    # Hent og print aggregeret win-rate
    conn2 = await asyncpg.connect(dsn=DB_DSN)
    stats = await conn2.fetchrow(
        """
        SELECT
            COUNT(*)                                       AS total,
            COUNT(*) FILTER (WHERE won = true)             AS won_count,
            COUNT(*) FILTER (WHERE won = false)            AS lost_count,
            COUNT(*) FILTER (WHERE won IS NULL)            AS pending_count,
            COALESCE(SUM(pnl_usdc), 0)                    AS total_pnl,
            COALESCE(SUM(size_filled * price), 0)          AS total_invested
        FROM copy_orders
        WHERE status IN ('paper', 'filled')
        """
    )
    by_outcome = await conn2.fetch(
        """
        SELECT
            UPPER(outcome)                                 AS outcome,
            COUNT(*)                                       AS total,
            COUNT(*) FILTER (WHERE won = true)             AS won_count,
            ROUND(AVG(price)::numeric, 4)                  AS avg_entry_price,
            ROUND(AVG(pnl_usdc)::numeric, 2)               AS avg_pnl
        FROM copy_orders
        WHERE won IS NOT NULL
        GROUP BY UPPER(outcome)
        ORDER BY total DESC
        """
    )
    daily = await conn2.fetch(
        """
        SELECT
            DATE(timestamp)                                AS day,
            COUNT(*)                                       AS trades,
            COUNT(*) FILTER (WHERE won = true)             AS won_count,
            COUNT(*) FILTER (WHERE won = false)            AS lost_count,
            ROUND(COALESCE(SUM(pnl_usdc), 0)::numeric, 2) AS pnl
        FROM copy_orders
        WHERE status IN ('paper', 'filled')
        GROUP BY DATE(timestamp)
        ORDER BY day DESC
        LIMIT 7
        """
    )
    await conn2.close()

    total = int(stats["total"] or 0)
    won = int(stats["won_count"] or 0)
    lost = int(stats["lost_count"] or 0)
    pending = int(stats["pending_count"] or 0)
    total_pnl = float(stats["total_pnl"] or 0)
    invested = float(stats["total_invested"] or 0)
    resolved_total = won + lost
    win_rate = won / resolved_total if resolved_total > 0 else 0
    roi = (total_pnl / invested * 100) if invested > 0 else 0

    print(f"\n📊 SAMLET STATISTIK ({total} trades)")
    print(f"   Win rate:       {win_rate:.1%}  ({won}W / {lost}L / {pending} afventer)")
    print(f"   Sim. P&L:       ${total_pnl:+,.2f} USDC")
    print(f"   Investeret:     ${invested:,.2f} USDC (sim.)")
    print(f"   ROI:            {roi:+.2f}%")

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
        print(f"   {'Dato':<12}  {'Trades':>7}  {'W':>5}  {'L':>5}  {'Win%':>6}  {'P&L':>12}")
        print("   " + "-" * 56)
        for row in daily:
            ot = int(row["won_count"]) + int(row["lost_count"])
            wr = int(row["won_count"]) / ot if ot > 0 else 0
            print(
                f"   {str(row['day']):<12}  {int(row['trades']):>7}  "
                f"{int(row['won_count']):>5}  {int(row['lost_count']):>5}  "
                f"{wr:>6.1%}  ${float(row['pnl']):>+10.2f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
