"""
tests/test_executor.py — Unit tests for executor.py og relaterede moduler.

Dækning:
  Gate tests (7)       — én test per gate
  Paper trading (3)    — DRY_RUN mode, go-live gate trigger
  Security (2)         — private key aldrig i logs, daily_stats atomisk

Ingen rigtige DB- eller API-kald — alt mockes.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from executor_types import OrderResult, TradeEvent

# ── Hjælpere ────────────────────────────────────────────────────────────────────


def _make_event(**kwargs: Any) -> TradeEvent:
    defaults = dict(
        id=1,
        wallet_id=10,
        wallet_address="0xABCD1234abcd1234abcd1234abcd1234abcd1234",
        wallet_label="test-whale",
        condition_id="0xcondition000000000000000000000000000001",
        outcome="Yes",
        event_type="opened",
        new_size=Decimal("100"),
        price_at_event=Decimal("0.65"),
    )
    defaults.update(kwargs)
    return TradeEvent(**defaults)


def _mock_conn(fetchrow_returns: dict | None = None, fetchval_returns: Any = None) -> MagicMock:
    """Simpel asyncpg.Connection mock med konfigurerbare fetchrow-svar."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_returns)
    conn.fetchval = AsyncMock(return_value=fetchval_returns)
    conn.execute = AsyncMock(return_value=None)
    return conn


# ── Gate tests ──────────────────────────────────────────────────────────────────


class TestGate1WalletFollowed:
    @pytest.mark.asyncio
    async def test_gate_1_wallet_not_followed(self) -> None:
        """Gate 1 afviser event hvis wallet ikke er på followed_wallets."""
        from executor_gates import _gate1_wallet_followed

        conn = _mock_conn(fetchrow_returns=None)  # ingen række = ikke fulgt
        event = _make_event()
        ok, reason = await _gate1_wallet_followed(conn, event)
        assert ok is False
        assert "ikke fulgt" in reason

    @pytest.mark.asyncio
    async def test_gate_1_wallet_followed_passes(self) -> None:
        """Gate 1 godkender hvis wallet er på followed_wallets."""
        from executor_gates import _gate1_wallet_followed

        conn = _mock_conn(fetchrow_returns={"exists": 1})
        event = _make_event()
        ok, reason = await _gate1_wallet_followed(conn, event)
        assert ok is True
        assert reason == ""


class TestGate2OnlyOpened:
    @pytest.mark.asyncio
    async def test_gate_2_only_opened_events(self) -> None:
        """Gate 2 afviser 'closed' og 'resized' event_types."""
        from executor_gates import _gate2_only_opened

        conn = _mock_conn()
        for bad_type in ("closed", "resized"):
            event = _make_event(event_type=bad_type)
            ok, reason = await _gate2_only_opened(conn, event)
            assert ok is False, f"Forventede afvisning for event_type={bad_type!r}"
            assert bad_type in reason

    @pytest.mark.asyncio
    async def test_gate_2_opened_passes(self) -> None:
        """Gate 2 godkender 'opened' event."""
        from executor_gates import _gate2_only_opened

        conn = _mock_conn()
        event = _make_event(event_type="opened")
        ok, reason = await _gate2_only_opened(conn, event)
        assert ok is True


class TestGate3NotExposed:
    @pytest.mark.asyncio
    async def test_gate_3_already_exposed(self) -> None:
        """Gate 3 afviser hvis copy_orders allerede har aktiv ordre på condition_id."""
        from executor_gates import _gate3_not_exposed

        conn = _mock_conn(fetchrow_returns={"exists": 1})
        event = _make_event()
        ok, reason = await _gate3_not_exposed(conn, event)
        assert ok is False
        assert "eksponeret" in reason

    @pytest.mark.asyncio
    async def test_gate_3_not_exposed_passes(self) -> None:
        """Gate 3 godkender hvis ingen eksisterende ordre i markedet."""
        from executor_gates import _gate3_not_exposed

        conn = _mock_conn(fetchrow_returns=None)
        event = _make_event()
        ok, reason = await _gate3_not_exposed(conn, event)
        assert ok is True


class TestGate4Spread:
    @pytest.mark.asyncio
    async def test_gate_4_spread_too_wide(self) -> None:
        """Gate 4 afviser hvis bid-ask spread >= 5%."""
        from executor_gates import _gate4_liquidity

        conn = _mock_conn(
            fetchrow_returns={
                "clob_token_ids": '["token123"]',
                "outcomes": '["Yes"]',
            }
        )
        # Spread = (0.70 - 0.60) / 0.70 = 14.3% → afvis
        mock_book = {"bids": [{"price": "0.60"}], "asks": [{"price": "0.70"}]}
        with patch("executor_gates.get_clob_orderbook", AsyncMock(return_value=mock_book)):
            event = _make_event()
            ok, reason = await _gate4_liquidity(conn, event)
        assert ok is False
        assert "spread" in reason

    @pytest.mark.asyncio
    async def test_gate_4_tight_spread_passes(self) -> None:
        """Gate 4 godkender hvis spread < 5%."""
        from executor_gates import _gate4_liquidity

        conn = _mock_conn(
            fetchrow_returns={
                "clob_token_ids": '["token123"]',
                "outcomes": '["Yes"]',
            }
        )
        # Spread = (0.66 - 0.65) / 0.66 = 1.5% → godkend
        mock_book = {"bids": [{"price": "0.65"}], "asks": [{"price": "0.66"}]}
        with patch("executor_gates.get_clob_orderbook", AsyncMock(return_value=mock_book)):
            event = _make_event()
            ok, reason = await _gate4_liquidity(conn, event)
        assert ok is True


class TestGate5MarketClose:
    @pytest.mark.asyncio
    async def test_gate_5_market_closes_soon(self) -> None:
        """Gate 5 afviser hvis markedet lukker inden for 2 timer."""
        from datetime import datetime, timedelta, timezone

        from executor_gates import _gate5_market_close

        # Returnér end_date 30 minutter fremme
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        mock_data = [{"endDate": soon}]
        with patch("executor_gates.httpx.AsyncClient") as MockClient:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_data
            mock_resp.raise_for_status = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            event = _make_event()
            ok, reason = await _gate5_market_close(_mock_conn(), event)
        assert ok is False
        assert "lukker" in reason

    @pytest.mark.asyncio
    async def test_gate_5_market_far_away_passes(self) -> None:
        """Gate 5 godkender hvis markedet lukker om > 2 timer."""
        from datetime import datetime, timedelta, timezone

        from executor_gates import _gate5_market_close

        far = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        mock_data = [{"endDate": far}]
        with patch("executor_gates.httpx.AsyncClient") as MockClient:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_data
            mock_resp.raise_for_status = MagicMock()
            MockClient.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
            )
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            event = _make_event()
            ok, reason = await _gate5_market_close(_mock_conn(), event)
        assert ok is True


class TestGate6SizeCap:
    @pytest.mark.asyncio
    async def test_gate_6_size_hard_cap(self) -> None:
        """Gate 6 capper position til 20% af available_cash og afviser for lille size."""
        from executor_gates import calculate_size

        # available_cash = 100, pct = 0.05 → size = 5 (under hard cap på 20)
        conn = _mock_conn(fetchrow_returns={"position_size_pct": None})
        with patch("executor_gates.get_clob_balance", AsyncMock(return_value=Decimal("100"))):
            size = await calculate_size(conn, wallet_id=10)
        assert size == Decimal("5.00")

    @pytest.mark.asyncio
    async def test_gate_6_hard_cap_20_pct(self) -> None:
        """Gate 6 capper ved 20% hvis per-wallet pct er over hard cap."""
        from executor_gates import calculate_size

        # position_size_pct = 0.50 → cappet til 20%
        conn = _mock_conn(fetchrow_returns={"position_size_pct": Decimal("0.50")})
        with patch("executor_gates.get_clob_balance", AsyncMock(return_value=Decimal("1000"))):
            size = await calculate_size(conn, wallet_id=10)
        assert size == Decimal("200")  # 20% af 1000


class TestGate7DailyLoss:
    @pytest.mark.asyncio
    async def test_gate_7_daily_loss_limit(self) -> None:
        """Gate 7 afviser hvis daily realized_pnl <= -MAX_DAILY_LOSS."""
        from executor_gates import _gate7_daily_loss

        conn = _mock_conn(fetchrow_returns={"realized_pnl": Decimal("-60")})
        event = _make_event()
        ok, reason = await _gate7_daily_loss(conn, event)
        assert ok is False
        assert "daglig tab" in reason

    @pytest.mark.asyncio
    async def test_gate_7_loss_within_limit_passes(self) -> None:
        """Gate 7 godkender hvis tab er inden for grænsen."""
        from executor_gates import _gate7_daily_loss

        conn = _mock_conn(fetchrow_returns={"realized_pnl": Decimal("-10")})
        event = _make_event()
        ok, reason = await _gate7_daily_loss(conn, event)
        assert ok is True


# ── Paper trading tests ─────────────────────────────────────────────────────────


class TestPaperTrading:
    @pytest.mark.asyncio
    async def test_dry_run_logs_paper_order(self) -> None:
        """DRY_RUN=True → status='paper' i copy_orders, ingen CLOB-kald."""
        import importlib
        import sys

        # Sørg for at DB_URL er sat så executor kan importeres uden fejl
        with patch.dict("os.environ", {"DB_URL": "postgresql://test/polymarket"}):
            if "executor" in sys.modules:
                import executor
            else:
                import executor

        executor._dry_run_state["active"] = True
        event = _make_event()

        executed_inserts: list[tuple] = []

        async def fake_log_copy_order(conn, ev, size, result):
            executed_inserts.append((ev.id, result.status))

        conn_mock = _mock_conn()
        conn_ctx = MagicMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
        conn_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("executor.passes_gates", AsyncMock(return_value=(True, ""))),
            patch(
                "executor.calculate_size",
                AsyncMock(return_value=Decimal("5")),
            ),
            patch("executor.acquire", return_value=conn_ctx),
            patch("executor.log_copy_order", fake_log_copy_order),
            patch("executor.send_telegram", AsyncMock()),
            patch("executor.check_go_live_gate", AsyncMock()),
            patch("executor.submit_to_clob", AsyncMock()) as mock_clob,
        ):
            await executor.process_trade_event(event)

        mock_clob.assert_not_called()
        assert len(executed_inserts) == 1
        assert executed_inserts[0][1] == "paper"

    @pytest.mark.asyncio
    async def test_go_live_gate_not_triggered_below_20(self) -> None:
        """check_go_live_gate sender ikke Telegram hvis < 20 paper trades."""
        from executor_telegram import check_go_live_gate

        conn = _mock_conn(fetchrow_returns={"total": 5})
        with patch("executor_telegram.send_approval_request", AsyncMock()) as mock_send:
            await check_go_live_gate(conn)
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_go_live_gate_triggers_at_threshold(self) -> None:
        """check_go_live_gate sender approval request ved win_rate > 52% over >= 20 trades."""
        from executor_telegram import check_go_live_gate

        # Trin 1: fetchrow returnerer total=25 (count)
        # Trin 2: fetchrow returnerer win_row med won/total
        call_count = 0
        results = [
            {"total": 25},
            {"total": 25, "won": 14},  # win_rate = 14/25 = 56%
        ]

        async def multi_fetchrow(*args, **kwargs):
            nonlocal call_count
            r = results[min(call_count, len(results) - 1)]
            call_count += 1
            return r

        conn = MagicMock()
        conn.fetchrow = multi_fetchrow

        with patch("executor_telegram.send_approval_request", AsyncMock()) as mock_send:
            await check_go_live_gate(conn)
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        win_rate = call_args[0][0]
        assert win_rate > 0.52


# ── Security tests ──────────────────────────────────────────────────────────────


class TestSecurity:
    @pytest.mark.asyncio
    async def test_private_key_not_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """POLYMARKET_PRIVATE_KEY må aldrig optræde i log output."""
        import executor
        import executor_clob

        fake_key = "0xDEADBEEF_SUPER_SECRET_PRIVATE_KEY_1234567890abcdef"

        with patch.dict("os.environ", {"POLYMARKET_PRIVATE_KEY": fake_key}):
            # Simulér en fejl i submit_to_clob
            with patch("executor_clob._resolve_token_id", AsyncMock(return_value=None)):
                with caplog.at_level(logging.DEBUG):
                    event = _make_event()
                    result = await executor_clob.submit_to_clob(event, Decimal("5"))

        assert result.status == "failed"
        # Nøglen må IKKE optræde i nogen log-linje
        for record in caplog.records:
            assert fake_key not in record.getMessage(), (
                f"SIKKERHEDSFEJL: Private key fundet i log: {record.getMessage()!r}"
            )

    @pytest.mark.asyncio
    async def test_daily_stats_updated_atomically(self) -> None:
        """log_copy_order opdaterer daily_stats med korrekt ON CONFLICT DO UPDATE."""
        from executor import log_copy_order

        conn = _mock_conn()
        event = _make_event()
        result = OrderResult(
            status="paper",
            size_filled=Decimal("5"),
            price=Decimal("0.65"),
            error_msg=None,
        )
        await log_copy_order(conn, event, Decimal("5"), result)

        # execute kaldt to gange: INSERT copy_orders + INSERT/UPDATE daily_stats
        assert conn.execute.await_count == 2

        # Anden execute-kald indeholder ON CONFLICT
        second_call_sql: str = conn.execute.call_args_list[1][0][0]
        assert "ON CONFLICT" in second_call_sql
        assert "DO UPDATE" in second_call_sql

        # paper_orders_count arg = 1 for paper, 0 for live
        second_call_args = conn.execute.call_args_list[1][0]
        assert second_call_args[2] == 1  # $2 = paper_orders_count
