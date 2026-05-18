"""
quick_scan.py — Scan en wallet uden DB-afhængighed.
Bruger positions endpoint (per-marked aggregeret) for præcis win rate.

Brug: python quick_scan.py 0xADRESSE

Kræver kun stdlib — ingen pip-packages.

Tidligere brugte dette script activity-endpointet (individuelle BUY-transaktioner)
hvor cashPnl altid er 0 for åbne positioner. Positions-endpointet giver én række
per marked med korrekt P&L for alle afsluttede markets — uafhængigt af handelsstil.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
import sys
import time
import urllib.request


# ── API helpers ────────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_positions(address: str) -> list[dict]:
    """
    Hent alle historiske positions via positions-endpointet.

    Positions = per-marked aggregeret data (én række pr. marked wallet'en har
    handlet). cashPnl er korrekt udfyldt for alle afsluttede markets uanset
    om positionen er lukket manuelt eller via market resolution.
    """
    all_positions: list[dict] = []
    offset = 0
    limit = 500
    while True:
        url = (
            f"{DATA_API}/positions"
            f"?user={address}&sizeThreshold=0&limit={limit}&offset={offset}"
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            print(f"  API fejl {e.code}: {e.read().decode()[:300]}")
            break
        batch = json.loads(body)
        if isinstance(batch, dict):
            batch = batch.get("data", [])
        if not batch:
            break
        all_positions.extend(batch)
        print(f"  Hentet {len(all_positions)} positions...", flush=True)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(1.5)
    return all_positions


# ── Scoring ────────────────────────────────────────────────────────────────────

def _safe_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _is_resolved(pos: dict) -> bool:
    """En position er afsluttet hvis cashPnl != 0 eller market er resolved."""
    if _safe_float(pos.get("cashPnl")) != 0.0:
        return True
    market = pos.get("market") or {}
    return bool(market.get("resolved") or market.get("closed"))


def score_positions(positions: list[dict]) -> dict:
    """
    Beregn metrics fra positions-data.

    Kun resolved positions indgår i win rate og P&L-beregninger.
    Pending positions tælles separat.
    """
    resolved = [p for p in positions if _is_resolved(p)]
    pending_count = len(positions) - len(resolved)

    if not resolved:
        return {
            "total": len(positions),
            "resolved": 0,
            "pending": pending_count,
            "won": 0,
            "win_rate": None,
            "sortino": None,
            "max_drawdown": None,
            "total_pnl": 0.0,
            "avg_pnl": None,
            "best_trade": None,
            "worst_trade": None,
            "biggest_single_win_pct": None,
        }

    pnls = [_safe_float(p.get("cashPnl")) for p in resolved]
    pct_returns = [_safe_float(p.get("percentPnl")) / 100.0 for p in resolved]

    won = sum(1 for pnl in pnls if pnl > 0)
    win_rate = won / len(resolved)
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(resolved)

    # ── Sortino ratio ──────────────────────────────────────────────────────────
    sortino: float | None = None
    if len(pct_returns) >= 2:
        downside = [r for r in pct_returns if r < 0]
        if not downside:
            sortino = 99.0
        else:
            ds_std = statistics.stdev(downside) if len(downside) > 1 else abs(downside[0])
            if ds_std > 0:
                sortino = (statistics.mean(pct_returns) * 52) / (ds_std * math.sqrt(52))

    # ── Max drawdown ───────────────────────────────────────────────────────────
    cum = list(itertools.accumulate(pnls))
    peak = cum[0]
    max_dd = 0.0
    for val in cum:
        peak = max(peak, val)
        if peak > 0:
            max_dd = max(max_dd, (peak - val) / peak)

    # ── Bedste / værste trade ──────────────────────────────────────────────────
    best = max(resolved, key=lambda p: _safe_float(p.get("cashPnl")))
    worst = min(resolved, key=lambda p: _safe_float(p.get("cashPnl")))

    def _fmt(p: dict) -> dict:
        return {
            "title": str(p.get("title") or p.get("conditionId", "?"))[:55],
            "outcome": str(p.get("outcome") or "?"),
            "pnl": _safe_float(p.get("cashPnl")),
            "pct": _safe_float(p.get("percentPnl")),
        }

    biggest_win_pct = max((_safe_float(p.get("percentPnl")) for p in resolved), default=None)

    return {
        "total": len(positions),
        "resolved": len(resolved),
        "pending": pending_count,
        "won": won,
        "win_rate": win_rate,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "best_trade": _fmt(best),
        "worst_trade": _fmt(worst),
        "biggest_single_win_pct": biggest_win_pct,
    }


# ── Output ─────────────────────────────────────────────────────────────────────

def print_scores(address: str, s: dict) -> None:
    wr = s["win_rate"]
    so = s["sortino"]
    dd = s["max_drawdown"]
    ap = s["avg_pnl"]
    bwp = s["biggest_single_win_pct"]

    print()
    print("─" * 60)
    print(f"  Wallet:          {address}")
    print("─" * 60)
    print(f"  Positions total: {s['total']}  "
          f"({s['resolved']} resolved, {s['pending']} pending)")
    print(f"  Won:             {s['won']} / {s['resolved']}")
    print(f"  Win rate:        {f'{wr*100:.1f}%' if wr is not None else 'N/A'}")
    print(f"  Total P&L:       ${s['total_pnl']:+,.2f} USDC")
    print(f"  Avg P&L/trade:   {f'${ap:+,.2f}' if ap is not None else 'N/A'}")
    print(f"  Sortino ratio:   {f'{so:.2f}' if so is not None else 'N/A'}")
    print(f"  Max drawdown:    {f'{dd*100:.1f}%' if dd is not None else 'N/A'}")
    print(f"  Største enkelt:  {f'+{bwp:.0f}%' if bwp is not None else 'N/A'}")
    if s.get("best_trade"):
        b = s["best_trade"]
        print(f"  Bedste trade:    {b['title']} [{b['outcome']}]")
        print(f"                   +${b['pnl']:,.0f} USDC  ({b['pct']:+.1f}%)")
    if s.get("worst_trade"):
        w = s["worst_trade"]
        print(f"  Værste trade:    {w['title']} [{w['outcome']}]")
        print(f"                   ${w['pnl']:,.0f} USDC  ({w['pct']:+.1f}%)")
    print("─" * 60)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Brug: python quick_scan.py 0xADRESSE")
        sys.exit(1)
    address = sys.argv[1]
    print(f"Scanner {address} via positions endpoint...")
    positions = fetch_positions(address)
    if not positions:
        print("Ingen positions fundet — tjek wallet-adressen.")
        sys.exit(0)
    print(f"Total: {len(positions)} positions\n")
    s = score_positions(positions)
    print_scores(address, s)
