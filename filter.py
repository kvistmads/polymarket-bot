"""
filter.py — Manuelt CLI-værktøj til at score og udvælge Polymarket-wallets.

Subkommandoer:
    scan <wallet>          Score én wallet via Polymarket Data API
    list [--min-sortino]   List aktive fulgte wallets med scores
    follow <wallet>        Tilføj wallet til followed_wallets
    unfollow <wallet>      Sæt unfollowed_at på aktiv wallet
    recalculate            Genberegn scores for alle aktive wallets
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import httpx
from dotenv import load_dotenv
from tabulate import tabulate  # type: ignore[import-untyped]

from db import close_pool
from filter_db import (
    follow_wallet,
    get_active_follow,
    get_active_wallets,
    get_followed_wallets_with_scores,
    get_wallet_label,
    save_scores,
    unfollow_wallet,
    upsert_wallet,
)
from filter_scores import WalletScores, calculate_scores

load_dotenv()

# DB_URL er konsumeret af db.py (get_pool) — valideres der ved første forbindelse.
DB_URL: str = os.getenv("DB_URL", "")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DATA_API: str = "https://data-api.polymarket.com"
_PAGE_SIZE: int = 500
_RATE_LIMIT_SLEEP: float = 2.0  # 0.5 req/s

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("filter")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _fetch_activity_page(
    client: httpx.AsyncClient, address: str, offset: int
) -> list[dict]:
    """Hent én page med activity fra Polymarket Data API."""
    url = f"{DATA_API}/activity"
    params: dict[str, str | int] = {
        "user": address,
        "limit": _PAGE_SIZE,
        "offset": offset,
    }
    resp = await client.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("data", [])


async def fetch_all_trades(address: str) -> list[dict]:
    """Hent alle historiske trades for én wallet med pagination og rate-limit."""
    trades: list[dict] = []
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            page = await _fetch_activity_page(client, address, offset)
            trades.extend(page)
            log.debug(
                "Fetched %d trades (offset=%d) for %s", len(page), offset, address
            )
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
            await asyncio.sleep(_RATE_LIMIT_SLEEP)
    log.info("Fetched %d trades total for %s", len(trades), address)
    return trades


# ---------------------------------------------------------------------------
# Formatering
# ---------------------------------------------------------------------------


def _fmt(value: float | None, decimals: int = 1, pct: bool = False) -> str:
    if value is None:
        return "N/A"
    if pct:
        return f"{value * 100:.{decimals}f}%"
    return f"{value:.{decimals}f}"


def _print_scores(address: str, label: str | None, scores: WalletScores) -> None:
    """Print score-oversigt til stdout."""
    tag = f"{label}  ({address})" if label else address
    bankroll = (
        f"${float(scores.estimated_bankroll):,.0f}"
        if scores.estimated_bankroll
        else "N/A"
    )
    print(f"\nWallet: {tag}")
    print(f"Trades total:     {scores.trades_total}")
    print(f"Win rate:         {_fmt(scores.win_rate, pct=True)}")
    print(f"Sortino ratio:    {_fmt(scores.sortino_ratio, 2)}")
    print(f"Max drawdown:     {_fmt(scores.max_drawdown, pct=True)}")
    print(
        f"Bull win rate:    {_fmt(scores.bull_win_rate, pct=True)}  |  "
        f"Bear win rate: {_fmt(scores.bear_win_rate, pct=True)}"
    )
    print(f"Consistency:      {_fmt(scores.consistency_score, pct=True)}")
    print(f"Sizing entropy:   {_fmt(scores.sizing_entropy, 2)}")
    print(f"Est. bankroll:   {bankroll}")
    print(f"ÅOP:             {_fmt(scores.annual_return_pct, 1)}%")


# ---------------------------------------------------------------------------
# Subkommandoer
# ---------------------------------------------------------------------------


async def cmd_scan(args: argparse.Namespace) -> None:
    """Scan og score én wallet. Skriv resultater til stdout + gem i wallet_scores."""
    address: str = args.wallet.lower()
    log.info("Scanning wallet %s …", address)
    trades = await fetch_all_trades(address)
    scores = calculate_scores(trades)
    wallet_id = await upsert_wallet(address, getattr(args, "label", None))
    await save_scores(wallet_id, scores)
    label = await get_wallet_label(address)
    _print_scores(address, label, scores)
    await close_pool()


async def cmd_list(args: argparse.Namespace) -> None:
    """List alle aktive fulgte wallets med deres seneste wallet_scores."""
    min_sortino: float | None = getattr(args, "min_sortino", None)
    rows = await get_followed_wallets_with_scores(min_sortino)
    if not rows:
        print("Ingen wallets matcher kriterierne.")
        await close_pool()
        return
    headers = [
        "Address",
        "Label",
        "Win rate",
        "Sortino",
        "Max DD",
        "Trades",
        "Size pct",
        "Scored at",
    ]
    table = [
        [
            r["address"][:10] + "…",
            r["label"] or "",
            _fmt(float(r["win_rate"]) if r["win_rate"] else None, pct=True),
            _fmt(float(r["sortino_ratio"]) if r["sortino_ratio"] else None, 2),
            _fmt(float(r["max_drawdown"]) if r["max_drawdown"] else None, pct=True),
            r["trades_total"] or 0,
            _fmt(
                float(r["position_size_pct"]) if r["position_size_pct"] else None,
                pct=True,
            ),
            str(r["last_scored_at"])[:16] if r["last_scored_at"] else "N/A",
        ]
        for r in rows
    ]
    print(tabulate(table, headers=headers, tablefmt="simple"))
    await close_pool()


async def cmd_follow(args: argparse.Namespace) -> None:
    """Tilføj wallet til followed_wallets. Opretter wallet-record hvis nødvendigt."""
    address: str = args.wallet.lower()
    label: str | None = getattr(args, "label", None)
    size_pct: float = getattr(args, "size_pct", 0.05)
    if not (0.01 <= size_pct <= 0.20):
        print(
            f"❌ Fejl: --size-pct {size_pct} er ugyldig. Skal være mellem 0.01 og 0.20."
        )
        return
    wallet_id = await upsert_wallet(address, label)
    existing = await get_active_follow(wallet_id)
    if existing:
        print(f"⚠️  Wallet {address} følges allerede aktivt.")
        await close_pool()
        return
    await follow_wallet(wallet_id, size_pct)
    tag = label or address
    print(f"✅ Følger nu {tag} ({address}) med size_pct={size_pct * 100:.1f}%")
    await close_pool()


async def cmd_unfollow(args: argparse.Namespace) -> None:
    """Sæt unfollowed_at på aktiv followed_wallets-række. Sletter ALDRIG data."""
    address: str = args.wallet.lower()
    reason: str | None = getattr(args, "reason", None)
    affected = await unfollow_wallet(address, reason)
    if affected == 0:
        print(f"⚠️  Wallet {address} følges ikke aktivt — ingen ændring foretaget.")
    else:
        print(
            f"✅ Stoppet med at følge {address}."
            + (f" Årsag: {reason}" if reason else "")
        )
    await close_pool()


async def cmd_recalculate(args: argparse.Namespace) -> None:
    """Genberegn scores for alle aktive fulgte wallets + gem snapshots."""
    wallets = await get_active_wallets()
    if not wallets:
        print("Ingen aktive wallets at genberegne.")
        await close_pool()
        return
    total = len(wallets)
    for idx, row in enumerate(wallets, start=1):
        label_tag = f" ({row['label']})" if row["label"] else ""
        print(f"[{idx}/{total}] Scanning {row['address']}{label_tag}…")
        try:
            trades = await fetch_all_trades(row["address"])
            scores = calculate_scores(trades)
            await save_scores(row["id"], scores)
            print(
                f"  ✓ {scores.trades_total} trades  "
                f"win_rate={_fmt(scores.win_rate, pct=True)}  "
                f"sortino={_fmt(scores.sortino_ratio, 2)}"
            )
        except Exception as exc:
            log.error("Fejl ved scanning af %s: %s", row["address"], exc)
            print(f"  ✗ Fejl: {exc}")
        if idx < total:
            await asyncio.sleep(_RATE_LIMIT_SLEEP)
    print(f"\nFærdig — {total} wallets genberegnet.")
    await close_pool()


# ---------------------------------------------------------------------------
# argparse CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Byg og returnér den fulde argparse parser med subcommands."""
    parser = argparse.ArgumentParser(
        prog="filter.py",
        description="Polymarket wallet filter & score CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Score én wallet")
    p_scan.add_argument("wallet", help="Wallet-adresse (0x…)")
    p_scan.add_argument("--label", default=None, help="Valgfrit label")
    p_scan.set_defaults(func=cmd_scan)

    p_list = sub.add_parser("list", help="List fulgte wallets")
    p_list.add_argument("--min-sortino", type=float, default=None, dest="min_sortino")
    p_list.set_defaults(func=cmd_list)

    p_follow = sub.add_parser("follow", help="Følg en wallet")
    p_follow.add_argument("wallet", help="Wallet-adresse (0x…)")
    p_follow.add_argument("--label", default=None)
    p_follow.add_argument("--size-pct", type=float, default=0.05, dest="size_pct")
    p_follow.set_defaults(func=cmd_follow)

    p_unfollow = sub.add_parser("unfollow", help="Stop med at følge en wallet")
    p_unfollow.add_argument("wallet", help="Wallet-adresse (0x…)")
    p_unfollow.add_argument("--reason", default=None)
    p_unfollow.set_defaults(func=cmd_unfollow)

    p_recalc = sub.add_parser("recalculate", help="Genberegn alle scores")
    p_recalc.set_defaults(func=cmd_recalculate)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    asyncio.run(args.func(args))
