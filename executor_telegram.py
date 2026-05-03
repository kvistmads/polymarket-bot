"""
executor_telegram.py — Telegram Bot integration for executor.

Eksponerer:
  send_telegram(text)                     → None
  send_approval_request(win_rate, total)  → None
  send_daily_summary(totals, today_count, by_outcome, top_market) → None
  telegram_polling_loop()                 → coroutine (kør som task)
  check_go_live_gate(conn)                → None

Opdaterer _dry_run_state i executor.py via delt dict-reference.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import asyncpg
import httpx

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Delt state-reference — injiceres fra executor.py ved import
_dry_run_state: dict[str, bool] = {"active": True}


def inject_dry_run_state(state: dict[str, bool]) -> None:
    """Kald fra executor.py for at dele DRY_RUN-referencen."""
    global _dry_run_state
    _dry_run_state = state


async def send_telegram(text: str) -> None:
    """Send en besked til Telegram-chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram ikke konfigureret — besked droppet: %s", text[:80])
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"),
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
    except Exception:
        log.exception("send_telegram fejlede")


async def send_approval_request(win_rate: float, total: int) -> None:
    """Send Telegram inline keyboard med go-live godkendelse."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage"),
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": (
                        f"🚀 <b>Bot klar til live trading!</b>\n"
                        f"Win rate: {win_rate:.1%} over {total} paper trades\n\n"
                        "Vil du aktivere live trading?"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "✅ Klar — gå live",
                                    "callback_data": "go_live",
                                },
                                {"text": "❌ Ikke klar", "callback_data": "stay_paper"},
                            ]
                        ]
                    },
                },
            )
    except Exception:
        log.exception("send_approval_request fejlede")


async def send_daily_summary(
    totals: dict,
    today_count: int,
    by_outcome: list[dict],
    top_market: str | None,
) -> None:
    """Send daglig opsummering til Telegram (kaldes kl. 06:00 UTC fra executor.py)."""
    total = int(totals.get("total") or 0)
    won = int(totals.get("won_count") or 0)
    lost = int(totals.get("lost_count") or 0)
    pending = int(totals.get("pending_count") or 0)
    total_pnl = float(totals.get("total_pnl") or 0)
    invested = float(totals.get("total_invested") or 0)

    resolved = won + lost
    win_rate = won / resolved if resolved > 0 else 0
    roi = (total_pnl / invested * 100) if invested > 0 else 0
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")

    lines = [
        f"📊 <b>Daglig opsummering</b> — {date_str}",
        "",
        f"🏆 <b>Win rate:</b> {win_rate:.1%}  ({won}W / {lost}L / {pending} afventer)",
        f"{pnl_emoji} <b>Sim. P&amp;L:</b> ${total_pnl:+.2f} USDC  (ROI {roi:+.1f}%)",
        f"🔢 <b>Trades i dag:</b> {today_count}  |  Total: {total}",
    ]

    if by_outcome:
        lines.append("")
        lines.append("🎯 <b>Win rate per outcome-type:</b>")
        for row in by_outcome:
            ot = int(row.get("total") or 0)
            ow = int(row.get("won_count") or 0)
            wr = ow / ot if ot > 0 else 0
            bar = "█" * round(wr * 10) + "░" * (10 - round(wr * 10))
            lines.append(f"  {row['outcome']:6s} {bar} {wr:.0%}  ({ow}/{ot})")

    if top_market:
        lines.append("")
        lines.append(f"🔥 <b>Mest aktivt marked (7d):</b> {top_market[:50]}")

    await send_telegram("\n".join(lines))


async def telegram_polling_loop() -> None:
    """Long-polling loop til Telegram callback_data (go_live / stay_paper)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                r = await client.get(
                    _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates"),
                    params={
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": ["callback_query"],
                    },
                )
                data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue
                action = cb.get("data")
                if action == "go_live":
                    _dry_run_state["active"] = False
                    log.info("🚀 Go-live godkendt via Telegram — DRY_RUN deaktiveret")
                    await send_telegram(
                        "✅ <b>Live trading aktiveret!</b> DRY_RUN=false"
                    )
                elif action == "stay_paper":
                    await send_telegram("📄 Paper trading fortsætter.")
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("telegram_polling_loop fejlede — genprøver om 10s")
            await asyncio.sleep(10)


async def check_go_live_gate(conn: asyncpg.Connection) -> None:
    """Send Telegram-godkendelse hvis win_rate > 52% over >= 20 paper trades."""
    count_row = await conn.fetchrow(
        "SELECT COUNT(*) AS total FROM copy_orders WHERE status = 'paper'"
    )
    total = count_row["total"] if count_row else 0
    if total < 20:
        return

    win_row = await conn.fetchrow("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE te.pnl_at_close > 0) AS won
        FROM copy_orders co
        JOIN trade_events te
          ON te.condition_id = co.condition_id AND te.event_type = 'closed'
        WHERE co.status = 'paper'
        """)
    if not win_row or not win_row["total"]:
        return

    win_rate = win_row["won"] / win_row["total"]
    if win_rate > 0.52:
        await send_approval_request(win_rate, int(win_row["total"]))
