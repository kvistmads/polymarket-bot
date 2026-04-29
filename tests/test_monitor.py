"""
tests/test_monitor.py — Unit tests for monitor.py

Bruger fixtures fra conftest.py.
Ingen rigtig database eller HTTP-kald — alt mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

# ── test_diff_positions ────────────────────────────────────────────────────────


def test_diff_positions_opened(mock_positions: list[dict]) -> None:
    """Ny position der ikke fandtes i old → opened."""
    from monitor import diff_positions

    opened, closed, changed = diff_positions([], mock_positions)
    assert len(opened) == 2
    assert len(closed) == 0
    assert len(changed) == 0


def test_diff_positions_closed(mock_positions: list[dict]) -> None:
    """Position der fandtes i old men ikke i new → closed."""
    from monitor import diff_positions

    opened, closed, changed = diff_positions(mock_positions, [])
    assert len(opened) == 0
    assert len(closed) == 2
    assert len(changed) == 0


def test_diff_positions_resized(mock_positions: list[dict]) -> None:
    """Position med ændret størrelse → changed."""
    from monitor import diff_positions

    old = [dict(p) for p in mock_positions]
    new = [dict(p) for p in mock_positions]
    new[0]["size"] = "150.0"  # ændret fra 100.0
    opened, closed, changed = diff_positions(old, new)
    assert len(opened) == 0
    assert len(closed) == 0
    assert len(changed) == 1
    assert changed[0][1]["size"] == "150.0"


def test_diff_positions_no_change(mock_positions: list[dict]) -> None:
    """Identiske lister → ingen events."""
    from monitor import diff_positions

    opened, closed, changed = diff_positions(mock_positions, mock_positions)
    assert opened == []
    assert closed == []
    assert changed == []


# ── test_w (wallet label) ─────────────────────────────────────────────────────


def test_w_with_label() -> None:
    """Når label er sat skal det bruges i stedet for adressen."""
    from monitor import _w

    assert (
        _w("0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf", "whale-001") == "[whale-001]"
    )


def test_w_without_label_falls_back_to_address() -> None:
    """Ingen label → kort wallet-prefix."""
    from monitor import _w

    assert _w("0x0b7a6030507efe5db145fbb57a25ba0c5f9d86cf") == "[0x0b7a…86cf]"


# ── test_fetch_positions_retry ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_positions_retry_on_429() -> None:
    """429-svar skal trigge 3 retry-forsøg med backoff."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    http_error = requests.HTTPError(response=mock_response)

    call_count = 0

    def flaky_fetch(wallet: str) -> list[dict]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise http_error
        return []  # succes på 3. forsøg

    with patch("monitor.fetch_positions", side_effect=flaky_fetch):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            from monitor import fetch_positions_with_retry

            result = await fetch_positions_with_retry("0xtest", max_attempts=3)

    assert call_count == 3
    assert result == []


@pytest.mark.asyncio
async def test_fetch_positions_retry_gives_up_after_max() -> None:
    """Alle forsøg fejler → returnér tom liste, ingen exception."""
    mock_response = MagicMock()
    mock_response.status_code = 429
    http_error = requests.HTTPError(response=mock_response)

    with patch("monitor.fetch_positions", side_effect=http_error):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            from monitor import fetch_positions_with_retry

            result = await fetch_positions_with_retry("0xtest", max_attempts=3)

    assert result == []


@pytest.mark.asyncio
async def test_fetch_positions_retry_4xx_no_retry() -> None:
    """4xx (non-429) HTTP-fejl skal IKKE forsøge igen — return tomt."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    http_error = requests.HTTPError(response=mock_response)

    call_count = 0

    def fail_fetch(wallet: str) -> list[dict]:
        nonlocal call_count
        call_count += 1
        raise http_error

    with patch("monitor.fetch_positions", side_effect=fail_fetch):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            from monitor import fetch_positions_with_retry

            result = await fetch_positions_with_retry("0xtest", max_attempts=3)

    assert call_count == 1
    assert result == []


# ── test_db_writes ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_insert_trade_event_opened(mock_positions: list[dict]) -> None:
    """opened-event indsætter korrekte værdier i trade_events."""
    from monitor import _db_insert_trade_event

    mock_conn = AsyncMock()
    pos = mock_positions[0]

    await _db_insert_trade_event(
        mock_conn, wallet_id=1, event_type="opened", new_pos=pos
    )

    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args[0]
    # call_args = (sql, $1, $2, $3, $4, $5, $6, $7, $8) — event_type er $4 → index 4
    assert call_args[4] == "opened"
    # pnl_at_close skal være None for opened-event ($8 → index 8)
    assert call_args[8] is None


@pytest.mark.asyncio
async def test_db_insert_trade_event_closed_has_pnl(mock_positions: list[dict]) -> None:
    """closed-event skal inkludere cashPnl som pnl_at_close."""
    from monitor import _db_insert_trade_event

    mock_conn = AsyncMock()
    pos = mock_positions[0]  # cashPnl = "7.00"

    await _db_insert_trade_event(
        mock_conn, wallet_id=1, event_type="closed", new_pos=pos
    )

    call_args = mock_conn.execute.call_args[0]
    assert call_args[4] == "closed"
    assert call_args[8] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_db_insert_trade_event_resized_has_old_size(
    mock_positions: list[dict],
) -> None:
    """resized-event skal inkludere old_size fra old_pos."""
    from monitor import _db_insert_trade_event

    mock_conn = AsyncMock()
    old_pos = mock_positions[0]  # size 100
    new_pos = dict(mock_positions[0])
    new_pos["size"] = "150.0"

    await _db_insert_trade_event(
        mock_conn, wallet_id=1, event_type="resized", new_pos=new_pos, old_pos=old_pos
    )

    call_args = mock_conn.execute.call_args[0]
    assert call_args[4] == "resized"
    # old_size er $5 → index 5
    assert call_args[5] == pytest.approx(100.0)
    # new_size er $6 → index 6
    assert call_args[6] == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_db_upsert_position(mock_positions: list[dict]) -> None:
    """upsert_position kalder conn.execute med INSERT...ON CONFLICT."""
    from monitor import _db_upsert_position

    mock_conn = AsyncMock()
    pos = mock_positions[0]

    await _db_upsert_position(mock_conn, wallet_id=1, pos=pos)

    mock_conn.execute.assert_called_once()
    sql = mock_conn.execute.call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "DO UPDATE" in sql


@pytest.mark.asyncio
async def test_db_mark_closed_uses_update(mock_positions: list[dict]) -> None:
    """mark_closed udfører UPDATE — ikke DELETE — og sætter status='closed'."""
    from monitor import _db_mark_closed

    mock_conn = AsyncMock()
    await _db_mark_closed(mock_conn, wallet_id=1, pos=mock_positions[0])

    sql = mock_conn.execute.call_args[0][0]
    assert "UPDATE positions" in sql
    assert "status = 'closed'" in sql
    assert "DELETE" not in sql.upper()


@pytest.mark.asyncio
async def test_get_or_create_wallet_id_returns_existing() -> None:
    """Hvis wallet allerede findes returneres dens id uden insert."""
    from monitor import _get_or_create_wallet_id

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 42}

    wallet_id = await _get_or_create_wallet_id(mock_conn, "0xabc")

    assert wallet_id == 42
    mock_conn.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_create_wallet_id_inserts_new() -> None:
    """Hvis wallet ikke findes inserts en ny række og returner ny id."""
    from monitor import _get_or_create_wallet_id

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    mock_conn.fetchval.return_value = 99

    wallet_id = await _get_or_create_wallet_id(mock_conn, "0xnew")

    assert wallet_id == 99
    mock_conn.fetchval.assert_called_once()


# ── test_decimal_conversion ────────────────────────────────────────────────────


def test_decimal_handles_none() -> None:
    from monitor import _decimal

    assert _decimal(None) is None


def test_decimal_handles_empty_string() -> None:
    from monitor import _decimal

    assert _decimal("") is None


def test_decimal_converts_string() -> None:
    from monitor import _decimal

    assert _decimal("0.65") == pytest.approx(0.65)


def test_decimal_converts_float() -> None:
    from monitor import _decimal

    assert _decimal(0.65) == pytest.approx(0.65)


def test_decimal_handles_garbage() -> None:
    """Ugyldig string returnerer None — ingen exception."""
    from monitor import _decimal

    assert _decimal("not-a-number") is None
