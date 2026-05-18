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
    Kør Dune-query med wallet som parameter og returnér rækker.

    Dune SQL-query (opret på dune.com, sæt ID i DUNE_QUERY_ID):
    ────────────────────────────────────────────────────────────
    -- Polymarket wallet historisk performance
    -- Parameter: wallet_address (tekst)
    WITH trades AS (
        SELECT
            t.condition_id,
            t.title,
            t.outcome,
            t.maker_amount_filled   AS usdc_in,
            t.taker_amount_filled   AS shares_out,
            t.type,                 -- 'BUY' | 'SELL' | 'REDEEM'
            t.cash_pnl,
            t.percent_pnl,
            t.timestamp
        FROM polymarket_polygon.trades t  -- eller det korrekte tabelnavn på Dune
        WHERE LOWER(t.proxy_wallet) = LOWER('{{wallet_address}}')
          AND t.type IN ('SELL', 'REDEEM')
          AND t.timestamp >= NOW() - INTERVAL '90' DAY
    )
    SELECT
        COUNT(*)                                    AS resolved_trades,
        SUM(CASE WHEN cash_pnl > 0 THEN 1 ELSE 0 END) AS won,
        AVG(CASE WHEN cash_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
        SUM(cash_pnl)                               AS total_pnl,
        AVG(cash_pnl)                               AS avg_pnl,
        AVG(percent_pnl)                            AS avg_pct_return,
        MAX(cash_pnl)                               AS best_pnl,
        MIN(cash_pnl)                               AS worst_pnl,
        MAX(percent_pnl)                            AS best_pct,
        MIN(percent_pnl)                            AS worst_pct
    FROM trades
    ────────────────────────────────────────────────────────────
    Bemærk: tabelnavn og feltnavne kan variere — tjek på dune.com/browse/tables
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


def score_from_dune_rows(rows: list[dict]) -> dict | None:
    """Byg score-dict fra Dune-aggregerede rækker (én række = ét resultat)."""
    if not rows:
        return None
    r = rows[0]  # Aggregeret query → én række
    try:
        total = int(r.get("resolved_trades") or 0)
        won = int(r.get("won") or 0)
        win_rate = float(r.get("win_rate") or 0)
        total_pnl = float(r.get("total_pnl") or 0)
        avg_pnl = float(r.get("avg_pnl") or 0)
        best_pct = float(r.get("best_pct") or 0)
        worst_pct = float(r.get("worst_pct") or 0)
        best_pnl = float(r.get("best_pnl") or 0)
        worst_pnl = float(r.get("worst_pnl") or 0)
    except (TypeError, ValueError):
        return None

    return {
        "total": total,
        "won": won,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "sortino": None,       # Kræver per-trade data — tilføj hvis query udvides
        "max_drawdown": None,  # Samme
        "best_trade": {"title": "(via Dune)", "outcome": "", "pnl": best_pnl, "pct": best_pct},
        "worst_trade": {"title": "(via Dune)", "outcome": "", "pnl": worst_pnl, "pct": worst_pct},
    }


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

    # Forsøg 1: Dune Analytics
    if DUNE_API_KEY and DUNE_QUERY_ID:
        print(f"Scanner {address} via Dune Analytics...")
        dune_rows = fetch_from_dune(address)
        if dune_rows is not None:
            scores = score_from_dune_rows(dune_rows)
            if scores:
                print_scores(address, scores, "Dune Analytics (fuld historik)")
                sys.exit(0)
        print("  Dune fejlede — skifter til Polymarket activity API\n")

    # Forsøg 2: Polymarket activity API (cashPnl-filtreret)
    print(f"Scanner {address} via Polymarket activity API (seneste afsluttede trades)...")
    trades = fetch_resolved_activity(address)
    if not trades:
        print("Ingen afsluttede trades fundet — tjek wallet-adressen.")
        sys.exit(0)
    scores = score_trades(trades)
    print_scores(address, scores, f"Polymarket API (seneste {scores['total']} afsluttede trades)")
