"""
quick_scan.py — Scan en wallet uden DB-afhængighed.

Bruger to datakilder i rækkefølge:
  1. Dune Analytics API (foretrukket) — fuld historik, ingen offset-begrænsning.
     Kræver DUNE_API_KEY i .env eller som env-var.
  2. Polymarket activity API (fallback) — filtrerer for cashPnl != 0 entries
     (SELL/REDEEM), som repræsenterer faktisk afsluttede trades.

Brug: python quick_scan.py 0xADRESSE

Kræver kun stdlib + eventuelt requests (allerede i requirements.txt).
"""

from __future__ import annotations

import itertools
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DUNE_API_KEY: str = os.getenv("DUNE_API_KEY", "")

# Dune query ID for Polymarket wallet scoring.
# Opret query på dune.com og indsæt ID her.
# Se README/kommentar nedenfor for SQL.
DUNE_QUERY_ID: str = os.getenv("DUNE_QUERY_ID", "")

DATA_API = "https://data-api.polymarket.com"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── Dune Analytics ─────────────────────────────────────────────────────────────

def _dune_post(path: str, body: dict) -> dict:
    """POST til Dune API v1."""
    url = f"https://api.dune.com/api/v1{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**_HEADERS, "x-dune-api-key": DUNE_API_KEY,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _dune_get(path: str) -> dict:
    """GET fra Dune API v1."""
    url = f"https://api.dune.com/api/v1{path}"
    req = urllib.request.Request(
        url, headers={**_HEADERS, "x-dune-api-key": DUNE_API_KEY}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_from_dune(address: str) -> list[dict] | None:
    """
    Kør Dune-query med wallet som parameter og returnér aktivitetsrækker.

    Dune giver os handelshistorik uden offset-begrænsning. Bemærk: Dunes
    polymarket_polygon.market_trades tabel har ikke forudberegnet P&L —
    det henter vi fra Polymarket activity API i stedet. Dune bruges til
    aktivitetsmetrics: volumen, antal handler, hvornår wallet sidst handlede.

    Dune SQL-query (opret på dune.com, sæt ID i DUNE_QUERY_ID):
    ────────────────────────────────────────────────────────────
    -- Polymarket wallet aktivitetsoversigt
    -- Officiel tabel: polymarket_polygon.market_trades
    -- Dokumentation: docs.dune.com/data-catalog/curated/prediction-markets/polymarket/market_trades
    --
    -- Parameter: wallet_address (tekst, fx '0xabc...')
    -- OBS: maker og taker er VARBINARY — kast til VARCHAR ved sammenligning.

    SELECT
        COUNT(*)                                                        AS total_trade_events,
        COUNT(DISTINCT CAST(condition_id AS VARCHAR))                   AS unique_markets,
        SUM(amount)                                                     AS total_volume_usd,
        AVG(amount)                                                     AS avg_trade_size_usd,
        MIN(block_time)                                                 AS first_trade,
        MAX(block_time)                                                 AS last_trade,
        COUNT(CASE WHEN block_time >= NOW() - INTERVAL '30' DAY THEN 1 END) AS trades_last_30d,
        COUNT(CASE WHEN block_time >= NOW() - INTERVAL '7'  DAY THEN 1 END) AS trades_last_7d
    FROM polymarket_polygon.market_trades
    WHERE
        block_time >= NOW() - INTERVAL '365' DAY
        AND (
            LOWER(CAST(maker AS VARCHAR)) = LOWER('{{wallet_address}}')
            OR LOWER(CAST(taker AS VARCHAR)) = LOWER('{{wallet_address}}')
        )
    ────────────────────────────────────────────────────────────
    """
    if not DUNE_API_KEY or not DUNE_QUERY_ID:
        return None

    print(f"  Kører Dune query {DUNE_QUERY_ID}...", flush=True)
    try:
        # Start execution
        exec_resp = _dune_post(
            f"/query/{DUNE_QUERY_ID}/execute",
            {"query_parameters": {"wallet_address": address}},
        )
        execution_id = exec_resp.get("execution_id")
        if not execution_id:
            print(f"  Dune: ingen execution_id — {exec_resp}")
            return None

        # Poll for result (max 60 sek)
        for attempt in range(20):
            time.sleep(3)
            status_resp = _dune_get(f"/execution/{execution_id}/status")
            state = status_resp.get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                result_resp = _dune_get(f"/execution/{execution_id}/results")
                rows = result_resp.get("result", {}).get("rows", [])
                print(f"  Dune: {len(rows)} rækker modtaget")
                return rows
            elif state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                print(f"  Dune fejl: {state}")
                return None
            print(f"  Dune: venter... ({state})", flush=True)

        print("  Dune: timeout efter 60 sek")
        return None
    except Exception as exc:
        print(f"  Dune fejl: {exc}")
        return None


# ── Polymarket activity fallback ───────────────────────────────────────────────

def fetch_resolved_activity(address: str) -> list[dict]:
    """
    Hent activity-entries med non-zero cashPnl (SELL/REDEEM-typer).

    BUY-entries har altid cashPnl=0 og filtreres fra. Kun afsluttede
    handler tæller i win rate-beregningen.

    Begrænsning: API returnerer max 3000 entries (nyeste først). Giver
    de seneste ~150-500 afsluttede trades afhængigt af wallet-volumen.
    """
    all_entries: list[dict] = []
    offset = 0
    limit = 500
    hit_limit = False

    while True:
        url = (
            f"{DATA_API}/activity"
            f"?user={address}&limit={limit}&offset={offset}"
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            if "3000" in err or e.code == 400:
                hit_limit = True
            else:
                print(f"  API fejl {e.code}: {err}")
            break
        batch: list[dict] = json.loads(body)
        if not batch:
            break
        all_entries.extend(batch)
        print(f"  Hentet {len(all_entries)} activity entries...", flush=True)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(1.5)

    if hit_limit:
        print(f"  (API-grænse på 3000 entries nået — viser de seneste {len(all_entries)})")

    # Filtrer: kun entries med faktisk P&L = afsluttede positioner
    resolved = [e for e in all_entries if _safe_float(e.get("cashPnl")) != 0.0]
    print(f"  Heraf {len(resolved)} afsluttede trades (non-zero cashPnl)")
    return resolved


# ── Scoring ────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def score_trades(trades: list[dict]) -> dict:
    """Beregn metrics fra liste af afsluttede trade-dicts (cashPnl != 0)."""
    if not trades:
        return {
            "total": 0, "won": 0, "win_rate": None,
            "total_pnl": 0.0, "avg_pnl": None,
            "sortino": None, "max_drawdown": None,
            "best_trade": None, "worst_trade": None,
        }

    pnls = [_safe_float(t.get("cashPnl")) for t in trades]
    pct_returns = [_safe_float(t.get("percentPnl")) / 100.0 for t in trades]

    won = sum(1 for p in pnls if p > 0)
    win_rate = won / len(trades)
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(trades)

    # Sortino
    sortino: float | None = None
    if len(pct_returns) >= 2:
        downside = [r for r in pct_returns if r < 0]
        if not downside:
            sortino = 99.0
        else:
            ds_std = statistics.stdev(downside) if len(downside) > 1 else abs(downside[0])
            if ds_std > 0:
                sortino = (statistics.mean(pct_returns) * 52) / (ds_std * math.sqrt(52))

    # Max drawdown
    cum = list(itertools.accumulate(pnls))
    peak = cum[0]
    max_dd = 0.0
    for val in cum:
        peak = max(peak, val)
        if peak > 0:
            max_dd = max(max_dd, (peak - val) / peak)

    def _fmt(t: dict) -> dict:
        return {
            "title": str(t.get("title") or t.get("conditionId", "?"))[:55],
            "outcome": str(t.get("outcome") or "?"),
            "pnl": _safe_float(t.get("cashPnl")),
            "pct": _safe_float(t.get("percentPnl")),
        }

    best = max(trades, key=lambda t: _safe_float(t.get("cashPnl")))
    worst = min(trades, key=lambda t: _safe_float(t.get("cashPnl")))

    return {
        "total": len(trades),
        "won": won,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "best_trade": _fmt(best),
        "worst_trade": _fmt(worst),
    }


def print_dune_activity(address: str, rows: list[dict]) -> None:
    """Print aktivitetsmetrics fra Dune (supplerer P&L fra activity API)."""
    if not rows:
        print("  Dune: ingen data returneret.")
        return
    r = rows[0]
    total = int(r.get("total_trade_events") or 0)
    markets = int(r.get("unique_markets") or 0)
    volume = _safe_float(r.get("total_volume_usd"))
    avg_size = _safe_float(r.get("avg_trade_size_usd"))
    last_30d = int(r.get("trades_last_30d") or 0)
    last_7d = int(r.get("trades_last_7d") or 0)
    first = str(r.get("first_trade") or "?")[:10]
    last = str(r.get("last_trade") or "?")[:10]

    print()
    print("─" * 62)
    print(f"  Wallet:          {address}")
    print(f"  Datakilde:       Dune Analytics (fuld historik, 365 dage)")
    print("─" * 62)
    print(f"  On-chain events: {total}  (på {markets} markeder)")
    print(f"  Total volumen:   ${volume:,.0f} USDC")
    print(f"  Gns. handl.str: ${avg_size:,.2f} USDC")
    print(f"  Seneste 30 dage: {last_30d} handlinger")
    print(f"  Seneste 7 dage:  {last_7d} handlinger")
    print(f"  Første handel:   {first}")
    print(f"  Seneste handel:  {last}")
    print("─" * 62)


# ── Output ─────────────────────────────────────────────────────────────────────

def print_scores(address: str, s: dict, source: str) -> None:
    wr = s["win_rate"]
    so = s["sortino"]
    dd = s["max_drawdown"]
    ap = s["avg_pnl"]

    print()
    print("─" * 62)
    print(f"  Wallet:          {address}")
    print(f"  Datakilde:       {source}")
    print("─" * 62)
    print(f"  Afsluttede:      {s['total']}  (heraf {s['won']} vandt)")
    print(f"  Win rate:        {f'{wr*100:.1f}%' if wr is not None else 'N/A'}")
    print(f"  Total P&L:       ${s['total_pnl']:+,.2f} USDC")
    print(f"  Avg P&L/trade:   {f'${ap:+,.2f}' if ap is not None else 'N/A'}")
    print(f"  Sortino ratio:   {f'{so:.2f}' if so is not None else 'N/A'}")
    print(f"  Max drawdown:    {f'{dd*100:.1f}%' if dd is not None else 'N/A'}")
    if s.get("best_trade"):
        b = s["best_trade"]
        label = f"{b['title']} [{b['outcome']}]" if b['outcome'] else b['title']
        print(f"  Bedste trade:    {label}")
        print(f"                   +${b['pnl']:,.0f} USDC  ({b['pct']:+.1f}%)")
    if s.get("worst_trade"):
        w = s["worst_trade"]
        label = f"{w['title']} [{w['outcome']}]" if w['outcome'] else w['title']
        print(f"  Værste trade:    {label}")
        print(f"                   ${w['pnl']:,.0f} USDC  ({w['pct']:+.1f}%)")
    print("─" * 62)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Brug: python quick_scan.py 0xADRESSE")
        sys.exit(1)

    address = sys.argv[1]

    # ── Del 1: P&L via Polymarket activity API (cashPnl forudberegnet af Polymarket) ──
    print(f"Henter P&L data via Polymarket activity API...")
    trades = fetch_resolved_activity(address)
    if trades:
        scores = score_trades(trades)
        print_scores(
            address, scores,
            f"Polymarket API — {scores['total']} afsluttede trades (seneste historik)",
        )
    else:
        print("  Ingen afsluttede trades fundet via activity API.")

    # ── Del 2: Aktivitetshistorik via Dune (ingen offset-begrænsning) ──────────────
    if DUNE_API_KEY and DUNE_QUERY_ID:
        print(f"\nHenter aktivitetsdata via Dune Analytics (fuld historik)...")
        dune_rows = fetch_from_dune(address)
        if dune_rows is not None:
            print_dune_activity(address, dune_rows)
        else:
            print("  Dune fejlede — kun activity API data tilgængeligt.")
    else:
        print("\n(Dune ikke konfigureret — tilføj DUNE_API_KEY + DUNE_QUERY_ID i .env for fuld historik)")

    if not trades:
        sys.exit(1)
