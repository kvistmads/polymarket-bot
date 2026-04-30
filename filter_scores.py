"""
filter_scores.py — Score calculation helpers for filter.py.

Alle metrics beregnes fra rå trade-data returneret af Polymarket Data API.
Ingen DB- eller HTTP-afhængigheder her — ren funktionel beregning.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

# Antal trades/år brugt til Sortino annualisering
_TRADES_PER_YEAR: int = 52


@dataclass
class WalletScores:
    """DTO med alle beregnede metrics for én wallet."""

    trades_total: int
    trades_won: int
    win_rate: float | None
    sortino_ratio: float | None
    max_drawdown: float | None
    bull_win_rate: float | None
    bear_win_rate: float | None
    consistency_score: float | None
    sizing_entropy: float | None
    estimated_bankroll: Decimal | None
    annual_return_pct: float | None


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Konvertér value til float — returnér default ved fejl."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _calc_win_rate(trades: list[dict]) -> tuple[int, int, float | None]:
    """Returnér (total, won, win_rate)."""
    if not trades:
        return 0, 0, None
    won = sum(1 for t in trades if _safe_float(t.get("cashPnl")) > 0)
    rate = won / len(trades)
    return len(trades), won, rate


def _calc_sortino(trades: list[dict]) -> float | None:
    """Annualiseret Sortino ratio (downside deviation)."""
    returns = [_safe_float(t.get("percentPnl")) / 100 for t in trades]
    if len(returns) < 2:
        return None
    downside = [r for r in returns if r < 0]
    if not downside:
        # Ingen negative returns → Sortino er uendelig (sæt høj sentinel)
        return 99.0
    downside_std = statistics.stdev(downside) if len(downside) > 1 else abs(downside[0])
    if downside_std == 0:
        return None
    avg_return = statistics.mean(returns)
    sortino = (avg_return * _TRADES_PER_YEAR) / (downside_std * (_TRADES_PER_YEAR**0.5))
    return sortino


def _calc_max_drawdown(trades: list[dict]) -> float | None:
    """Største fald fra peak til trough i kumulativ P&L."""
    if not trades:
        return None
    cumulative = list(
        itertools.accumulate(_safe_float(t.get("cashPnl")) for t in trades)
    )
    peak = cumulative[0]
    max_dd = 0.0
    for val in cumulative:
        peak = max(peak, val)
        if peak > 0:
            dd = (peak - val) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _calc_bull_bear(
    trades: list[dict],
) -> tuple[float | None, float | None]:
    """Returnér (bull_win_rate, bear_win_rate). Bull = Yes, Bear = No."""
    bull = [t for t in trades if t.get("outcome", "").lower() == "yes"]
    bear = [t for t in trades if t.get("outcome", "").lower() == "no"]
    bull_wr = (
        sum(1 for t in bull if _safe_float(t.get("cashPnl")) > 0) / len(bull)
        if bull
        else None
    )
    bear_wr = (
        sum(1 for t in bear if _safe_float(t.get("cashPnl")) > 0) / len(bear)
        if bear
        else None
    )
    return bull_wr, bear_wr


def _calc_consistency(bull_wr: float | None, bear_wr: float | None) -> float | None:
    """1.0 - |bull_wr - bear_wr|. None hvis kun én retning er handlet."""
    if bull_wr is None or bear_wr is None:
        return None
    return 1.0 - abs(bull_wr - bear_wr)


def _calc_sizing_entropy(trades: list[dict]) -> float | None:
    """Normaliseret Shannon entropy af position-størrelser (0=ensartet, 1=kaotisk)."""
    sizes = [
        _safe_float(t.get("size")) for t in trades if _safe_float(t.get("size")) > 0
    ]
    if not sizes:
        return None
    total = sum(sizes)
    if total == 0:
        return None
    probs = [s / total for s in sizes]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    max_entropy = math.log2(len(sizes)) if len(sizes) > 1 else 1.0
    return entropy / max_entropy if max_entropy > 0 else 0.0


def _calc_annual_return(
    trades: list[dict], estimated_bankroll: Decimal | None
) -> float | None:
    """ÅOP: total P&L / bankroll × (365 / historik_dage)."""
    if not trades or estimated_bankroll is None or estimated_bankroll <= 0:
        return None
    total_pnl = sum(_safe_float(t.get("cashPnl")) for t in trades)
    # Prøv at estimere antal dage i historikken
    timestamps = []
    for t in trades:
        ts = t.get("timestamp") or t.get("createdAt")
        if ts:
            try:
                timestamps.append(float(ts))
            except (TypeError, ValueError):
                pass
    if len(timestamps) >= 2:
        days = (max(timestamps) - min(timestamps)) / 86400
    else:
        days = 30.0  # fallback: antag 30 dage
    if days < 1:
        days = 1.0
    return (total_pnl / float(estimated_bankroll)) * (365.0 / days) * 100


def calculate_scores(trades: list[dict]) -> WalletScores:
    """Beregn alle metrics fra en liste af trade-dicts (Polymarket Data API format).

    Args:
        trades: Liste af dicts fra GET /activity endpoint.

    Returns:
        WalletScores dataclass med alle beregnede metrics.
    """
    if not trades:
        return WalletScores(
            trades_total=0,
            trades_won=0,
            win_rate=None,
            sortino_ratio=None,
            max_drawdown=None,
            bull_win_rate=None,
            bear_win_rate=None,
            consistency_score=None,
            sizing_entropy=None,
            estimated_bankroll=None,
            annual_return_pct=None,
        )

    total, won, win_rate = _calc_win_rate(trades)
    sortino = _calc_sortino(trades)
    max_dd = _calc_max_drawdown(trades)
    bull_wr, bear_wr = _calc_bull_bear(trades)
    consistency = _calc_consistency(bull_wr, bear_wr)
    entropy = _calc_sizing_entropy(trades)

    # Estimeret bankroll: sum af current_value for alle trades
    total_value: Decimal = sum(
        (Decimal(str(t.get("currentValue") or t.get("size") or 0)) for t in trades),
        Decimal(0),
    )
    bankroll: Decimal | None = total_value if total_value > 0 else None

    annual_ret = _calc_annual_return(trades, bankroll)

    log.debug(
        "Scores beregnet: %d trades, win_rate=%.3f, sortino=%.3f",
        total,
        win_rate or 0,
        sortino or 0,
    )

    return WalletScores(
        trades_total=total,
        trades_won=won,
        win_rate=win_rate,
        sortino_ratio=sortino,
        max_drawdown=max_dd,
        bull_win_rate=bull_wr,
        bear_win_rate=bear_wr,
        consistency_score=consistency,
        sizing_entropy=entropy,
        estimated_bankroll=bankroll,
        annual_return_pct=annual_ret,
    )
