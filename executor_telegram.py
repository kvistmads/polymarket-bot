"""
executor_telegram.py — Telegram Bot integration for executor.

Eksponerer:
  send_telegram(text)                     → None
  send_approval_request(win_rate, total)  → None
  send_daily_summary(totals, today_count, top_market, per_wallet) → None
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
    top_market: str | None,
    per_wallet: list[dict] | None = None,
) -> None:
    """Send daglig opsummering til Telegram (kaldes kl. 06:00 UTC fra executor.py).

    Viser kun tal for aktuelt fulgte wallets (filtreret i executor.py).
    """
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

    # ── Per-wallet opdeling (vises altid når der er data) ──
    if per_wallet:
        lines.append("")
        lines.append("👛 <b>Per wallet:</b>")
        for w in per_wallet:
            w_won = int(w.get("won_count") or 0)
            w_lost = int(w.get("lost_count") or 0)
            w_pnl = float(w.get("total_pnl") or 0)
            w_inv = float(w.get("total_invested") or 0)
            w_today = int(w.get("today_count") or 0)
            w_res = w_won + w_lost
            w_wr = w_won / w_res if w_res > 0 else 0
            w_roi = (w_pnl / w_inv * 100) if w_inv > 0 else 0
            w_emoji = "📈" if w_pnl >= 0 else "📉"
            lines.append(
                f"  <b>{w['tag']}</b>  {w_wr:.0%} WR · "
                f"{w_emoji}${w_pnl:+.2f} (ROI {w_roi:+.1f}%) · "
                f"{w_today} i dag"
            )

    if top_market:
        lines.append("")
        lines.append(f"🔥 <b>Mest aktivt marked (7d):</b> {top_market[:50]}")

    await send_telegram("\n".join(lines))


# Kommando-handlers registreret af executor.py
_command_handlers: dict[str, object] = {}


def register_command(cmd: str, handler: object) -> None:
    """Registrér en async handler for en Telegram-kommando (f.eks. '/portfolio')."""
    _command_handlers[cmd] = handler


async def telegram_polling_loop() -> None:
    """Long-polling loop til Telegram callback_data og tekst-kommandoer."""
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
                        "allowed_updates": ["callback_query", "message"],
                    },
                )
                data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1

                # Håndtér inline-knapper (go_live / stay_paper)
                cb = update.get("callback_query")
                if cb:
                    action = cb.get("data")
                    if action == "go_live":
                        _dry_run_state["active"] = False
                        log.info("🚀 Go-live godkendt via Telegram — DRY_RUN deaktiveret")
                        await send_telegram("✅ <b>Live trading aktiveret!</b> DRY_RUN=false")
                    elif action == "stay_paper":
                        await send_telegram("📄 Paper trading fortsætter.")
                    continue

                # Håndtér tekst-kommandoer (/portfolio osv.)
                msg = update.get("message", {})
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    cmd = text.split()[0].lower().split("@")[0]  # /portfolio@botname → /portfolio
                    handler = _command_handlers.get(cmd)
                    if handler:
                        try:
                            await handler()  # type: ignore[operator]
                        except Exception:
                            log.exception("Kommando-handler %s fejlede", cmd)

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("telegram_polling_loop fejlede — genprøver om 10s")
            await asyncio.sleep(10)


async def check_go_live_gate(conn: asyncpg.Connection) -> tuple[float, int] | None:
    """Returnerer (win_rate, total) hvis >= 20 resolvede paper trades og win_rate >= 60%.

    Bruger copy_orders.won (sat af win_rate_tracker) — kræver IKKE JOIN på trade_events.
    Returnerer None hvis betingelserne ikke er opfyldt.
    Kalderen (process_trade_event) sender Telegram-godkendelse baseret på returværdien.
    """
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*)                            AS total,
            COUNT(*) FILTER (WHERE won = true)  AS won
        FROM copy_orders
        WHERE status = 'paper'
          AND won IS NOT NULL
        """
    )
    if not row or not row["total"]:
        return None
    total = int(row["total"])
    if total < 20:
        return None
    win_rate = int(row["won"]) / total
    if win_rate >= 0.60:
        return (win_rate, total)
    return None
