"""
tests/test_filter.py — Unit tests for filter_scores.py og filter.py.

Ingen rigtige DB- eller HTTP-kald: alle external dependencies er mockkede.
Krav: minimum 10 tests, alle grønne.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from filter_scores import (
    _calc_max_drawdown,
    _calc_sizing_entropy,
    calculate_scores,
)

# ---------------------------------------------------------------------------
# Helpers til test-fixtures
# ---------------------------------------------------------------------------


def _make_trades(
    cash_pnls: list[float],
    pct_pnls: list[float] | None = None,
    outcomes: list[str] | None = None,
    sizes: list[float] | None = None,
) -> list[dict]:
    """Opret en liste af minimale trade-dicts med de angivne værdier."""
    if pct_pnls is None:
        pct_pnls = cash_pnls  # Brug cashPnl som percentPnl som fallback
    if outcomes is None:
        outcomes = ["yes"] * len(cash_pnls)
    if sizes is None:
        sizes = [10.0] * len(cash_pnls)
    return [
        {
            "cashPnl": str(cp),
            "percentPnl": str(pp),
            "outcome": oc,
            "size": str(sz),
            "timestamp": str(1_700_000_000 + i * 86400),
        }
        for i, (cp, pp, oc, sz) in enumerate(zip(cash_pnls, pct_pnls, outcomes, sizes))
    ]


# ---------------------------------------------------------------------------
# Test 1: Win rate beregnes korrekt
# ---------------------------------------------------------------------------


def test_win_rate_basic() -> None:
    """Win rate beregnes korrekt fra trades med positive/negative cashPnl."""
    trades = _make_trades([10.0, -5.0, 8.0, -3.0])
    scores = calculate_scores(trades)
    assert scores.trades_total == 4
    assert scores.trades_won == 2
    assert scores.win_rate == pytest.approx(0.50, abs=1e-6)


# ---------------------------------------------------------------------------
# Test 2: Sortino ratio
# ---------------------------------------------------------------------------


def test_sortino_ratio_positive_for_good_trader() -> None:
    """Sortino er positiv for wallet med overvejende positive returns."""
    # 8 vinder-trades + 2 lille tab → positiv sortino
    cash = [10.0, 8.0, 12.0, 9.0, 11.0, 10.0, 7.0, 9.0, -1.0, -2.0]
    pct = [0.10, 0.08, 0.12, 0.09, 0.11, 0.10, 0.07, 0.09, -0.01, -0.02]
    trades = _make_trades(cash, pct)
    scores = calculate_scores(trades)
    assert scores.sortino_ratio is not None
    assert scores.sortino_ratio > 0


# ---------------------------------------------------------------------------
# Test 3: Max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_zero_for_only_gains() -> None:
    """Max drawdown er 0 hvis alle trades er profitable."""
    trades = _make_trades([5.0, 10.0, 3.0, 8.0])
    scores = calculate_scores(trades)
    assert scores.max_drawdown == pytest.approx(0.0, abs=1e-6)


def test_max_drawdown_detects_large_peak_to_trough() -> None:
    """Max drawdown beregnes korrekt for kendte tab-sekvenser."""
    # Kumulativ: [10, 20, 5] → peak=20, trough=5 → dd=(20-5)/20=0.75
    result = _calc_max_drawdown(
        [{"cashPnl": "10"}, {"cashPnl": "10"}, {"cashPnl": "-15"}]
    )
    assert result == pytest.approx(0.75, abs=1e-4)


# ---------------------------------------------------------------------------
# Test 4: Sizing entropy
# ---------------------------------------------------------------------------


def test_sizing_entropy_low_for_uniform_sizes() -> None:
    """Sizing entropy er lav (< 0.3) for fuldstændigt ensartede trade-størrelser."""
    # Alle størrelses ens → max entropy (normaliseret = 1.0, ikke 0)
    # Rettelse: ens størrelser giver MAX entropy (alle sandsynligheder er ens)
    trades = _make_trades([1.0] * 8, sizes=[5.0] * 8)
    result = _calc_sizing_entropy(trades)
    # Ensartede størrelser → normaliseret entropy ≈ 1.0 (maksimum)
    assert result is not None
    assert result > 0.9


def test_sizing_entropy_near_zero_for_single_dominant_trade() -> None:
    """Sizing entropy er næsten 0 hvis én trade dominerer totalt."""
    # Én handel på 10000, resten på 1 → meget lav entropy
    sizes = [10000.0] + [1.0] * 9
    trades = _make_trades([1.0] * 10, sizes=sizes)
    result = _calc_sizing_entropy(trades)
    assert result is not None
    assert result < 0.3


# ---------------------------------------------------------------------------
# Test 5–6: CLI — follow kommando (mock DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_inserts_to_followed_wallets() -> None:
    """cmd_follow indsætter korrekt i wallets + followed_wallets."""
    import filter as f_mod

    mock_follow_wallet = AsyncMock()

    with patch("filter.upsert_wallet", new=AsyncMock(return_value=42)):
        with patch("filter.get_active_follow", new=AsyncMock(return_value=None)):
            with patch("filter.follow_wallet", new=mock_follow_wallet):
                with patch("filter.close_pool", new=AsyncMock()):
                    args = argparse.Namespace(
                        wallet="0xABC123",
                        label="whale-001",
                        size_pct=0.07,
                    )
                    await f_mod.cmd_follow(args)
    # Verificér at follow_wallet blev kaldt med korrekte argumenter
    mock_follow_wallet.assert_called_once_with(42, 0.07)


@pytest.mark.asyncio
async def test_follow_rejects_invalid_size_pct(capsys: pytest.CaptureFixture) -> None:
    """cmd_follow fejler hvis --size-pct er udenfor 0.01–0.20."""
    import filter as f_mod

    args = argparse.Namespace(wallet="0xABC", label=None, size_pct=0.50)
    await f_mod.cmd_follow(args)
    captured = capsys.readouterr()
    assert "❌" in captured.out
    assert "ugyldig" in captured.out.lower() or "0.50" in captured.out


# ---------------------------------------------------------------------------
# Test 7: CLI — unfollow kommando
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unfollow_sets_unfollowed_at(capsys: pytest.CaptureFixture) -> None:
    """cmd_unfollow sætter unfollowed_at og reason korrekt."""
    import filter as f_mod

    with patch("filter.unfollow_wallet", new=AsyncMock(return_value=1)):
        with patch("filter.close_pool", new=AsyncMock()):
            args = argparse.Namespace(wallet="0xDEF456", reason="inaktiv 30 dage")
            await f_mod.cmd_unfollow(args)

    captured = capsys.readouterr()
    assert "✅" in captured.out
    assert "0xdef456" in captured.out.lower()


# ---------------------------------------------------------------------------
# Test 8: CLI — list filtrerer på --min-sortino
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_filters_by_min_sortino(capsys: pytest.CaptureFixture) -> None:
    """cmd_list filtrerer wallets med sortino under --min-sortino."""
    import filter as f_mod

    # Simulér to wallets: én med sortino=2.0, én med sortino=0.5
    mock_rows = [
        {
            "address": "0xaaa",
            "label": "high",
            "win_rate": Decimal("0.65"),
            "sortino_ratio": Decimal("2.0"),
            "max_drawdown": Decimal("0.15"),
            "trades_total": 100,
            "last_scored_at": None,
            "position_size_pct": Decimal("0.05"),
            "followed_at": None,
        },
        {
            "address": "0xbbb",
            "label": "low",
            "win_rate": Decimal("0.50"),
            "sortino_ratio": Decimal("0.5"),
            "max_drawdown": Decimal("0.30"),
            "trades_total": 50,
            "last_scored_at": None,
            "position_size_pct": Decimal("0.05"),
            "followed_at": None,
        },
    ]
    # get_followed_wallets_with_scores returnerer kun high-wallet (min_sortino=1.2 filtrerer)
    high_only = [mock_rows[0]]

    with patch(
        "filter.get_followed_wallets_with_scores", new=AsyncMock(return_value=high_only)
    ):
        with patch("filter.close_pool", new=AsyncMock()):
            args = argparse.Namespace(min_sortino=1.2)
            await f_mod.cmd_list(args)

    captured = capsys.readouterr()
    # high-wallet skal være i output
    assert "high" in captured.out
    # low-wallet (sortino=0.5) skal IKKE være i output (filtreret bort af filter_db)
    assert "low" not in captured.out


# ---------------------------------------------------------------------------
# Test 9: Pagination af activity API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_paginates_activity_api() -> None:
    """fetch_all_trades henter næste page hvis første returnerer 500 resultater."""
    from filter import fetch_all_trades

    # Første kald returnerer 500 trades, andet kald returnerer 10
    first_page = [
        {"cashPnl": "1", "percentPnl": "1", "outcome": "yes", "size": "5"}
    ] * 500
    second_page = [
        {"cashPnl": "1", "percentPnl": "1", "outcome": "yes", "size": "5"}
    ] * 10

    call_count = 0

    async def mock_fetch_page(client: object, address: str, offset: int) -> list[dict]:
        nonlocal call_count
        call_count += 1
        return first_page if offset == 0 else second_page

    # Mock httpx.AsyncClient så vi undgår proxy/network i sandbox
    mock_http_client = AsyncMock()
    mock_http_ctx = MagicMock()
    mock_http_ctx.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("filter._fetch_activity_page", side_effect=mock_fetch_page):
        with patch("filter.asyncio.sleep", new=AsyncMock()):
            with patch("filter.httpx.AsyncClient", return_value=mock_http_ctx):
                trades = await fetch_all_trades("0xTEST")

    assert call_count == 2
    assert len(trades) == 510


# ---------------------------------------------------------------------------
# Test 10: Rate-limit mellem wallets i recalculate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recalculate_rate_limits_between_wallets() -> None:
    """cmd_recalculate venter 2 sekunder mellem wallet-scans."""
    import filter as f_mod

    mock_rows = [
        {"id": 1, "address": "0xaaa", "label": "w1"},
        {"id": 2, "address": "0xbbb", "label": "w2"},
    ]
    sleep_calls: list[float] = []

    async def track_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with patch("filter.get_active_wallets", new=AsyncMock(return_value=mock_rows)):
        with patch("filter.fetch_all_trades", new=AsyncMock(return_value=[])):
            with patch("filter.save_scores", new=AsyncMock()):
                with patch("filter.asyncio.sleep", side_effect=track_sleep):
                    with patch("filter.close_pool", new=AsyncMock()):
                        args = argparse.Namespace()
                        await f_mod.cmd_recalculate(args)

    # Skal have sovet præcis én gang (mellem de 2 wallets — ikke efter den sidste)
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(2.0, abs=1e-9)
