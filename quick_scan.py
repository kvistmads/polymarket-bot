"""
quick_scan.py — Scan en wallet uden DB-afhængighed.
Brug: python quick_scan.py 0xADRESSE

Kræver: pip install httpx
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

from filter_scores import calculate_scores


def fetch_trades(address: str) -> list[dict]:
    trades: list[dict] = []
    offset = 0
    limit = 100
    while True:
        url = (
            f"https://data-api.polymarket.com/activity"
            f"?user={address}&limit={limit}&offset={offset}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            print(f"  API fejl {e.code}: {e.read().decode()[:300]}")
            break
        batch: list[dict] = json.loads(body)
        if not batch:
            break
        trades.extend(batch)
        print(f"  Hentet {len(trades)} trades...", flush=True)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(2)
    return trades


def print_scores(address: str, s: object) -> None:
    print()
    print(f"{'Wallet:':<22} {address}")
    print(f"{'Trades total:':<22} {s.trades_total}")
    print(f"{'Trades won:':<22} {s.trades_won}")
    print(f"{'Win rate:':<22} {f'{s.win_rate*100:.1f}%' if s.win_rate is not None else 'N/A'}")
    print(f"{'Sortino ratio:':<22} {f'{s.sortino_ratio:.2f}' if s.sortino_ratio is not None else 'N/A'}")
    print(f"{'Max drawdown:':<22} {f'{s.max_drawdown*100:.1f}%' if s.max_drawdown is not None else 'N/A'}")
    print(f"{'Bull win rate:':<22} {f'{s.bull_win_rate*100:.1f}%' if s.bull_win_rate is not None else 'N/A'}")
    print(f"{'Bear win rate:':<22} {f'{s.bear_win_rate*100:.1f}%' if s.bear_win_rate is not None else 'N/A'}")
    print(f"{'Consistency:':<22} {f'{s.consistency_score*100:.1f}%' if s.consistency_score is not None else 'N/A'}")
    print(f"{'Sizing entropy:':<22} {f'{s.sizing_entropy:.3f}' if s.sizing_entropy is not None else 'N/A'}")
    print(f"{'Est. bankroll:':<22} {f'${s.estimated_bankroll:,.0f}' if s.estimated_bankroll else 'N/A'}")
    print(f"{'ÅOP:':<22} {f'{s.annual_return_pct:+.1f}%' if s.annual_return_pct is not None else 'N/A'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Brug: python quick_scan.py 0xADRESSE")
        sys.exit(1)
    address = sys.argv[1]
    print(f"Scanner {address}...")
    trades = fetch_trades(address)
    print(f"Total: {len(trades)} trades\n")
    scores = calculate_scores(trades)
    print_scores(address, scores)
