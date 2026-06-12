"""
executor_telegram.py — Telegram Bot integration for executor.

Eksponerer:
  send_telegram(text, mode)               → None  (mode='paper'|'live')
  send_approval_request(wallet_id, ...)   → None
  send_daily_summary(totals, ...)         → None
  telegram_polling_loop()                 → coroutine
  check_go_live_gate(conn)                → list[dict] (per-wallet)

Dual-track: paper-wallets → TELEGRAM_BOT_TOKEN (paper bot)
            live-wallets  → TELEGRAM_BOT_TOKEN_LIVE (live bot)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import asyncpg
import httpx

from db import acquire

log = logging.getLogger(__name__)

# Paper bot (eksisterende)
TELEGRAM_BOT_TOKEN: str  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str    = os.environ.get("TELEGRAM_CHAT_ID", "")

# Live bot (ny)
TELEGRAM_BOT_TOKEN_LIVE: str = os.environ.get("TELEGRAM_BOT_TOKEN_LIVE", "")
TELEGRAM_CHAT_ID_LIVE: str   = os.environ.get("TELEGRAM_CHAT_ID_LIVE", "")

_TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Delt state-reference — injiceres fra executor.py ved import
_dry_run_state: dict[str, bool] = {"active": True}


def inject_dry_run_state(state: dict[str, bool]) -> None:
    """Kald fra executor.py for at dele DRY_RUN-referencen."""
    global _dry_run_state
    _dry_run_state = state


async def _send_to_bot(token: str, chat_id: str, text: str, extra: dict | None = None) -> None:
    """Intern helper — sender besked til en specifik bot."""
    if not token or not chat_id:
        log.debug("Telegram bot ikke konfigureret — besked droppet: %s", text[:80])
        return
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if extra:
        payload.update(extra)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                _TELEGRAM_API.format(token=token, method="sendMessage"),
                json=payload,
            )
    except Exception:
        log.exception("_send_to_bot fejlede (chat_id=%s)", chat_id)


async def send_telegram(text: str, mode: str = "paper") -> None:
    """Send besked til korrekt bot baseret på mode ('paper' eller 'live')."""
    if mode == "live" and TELEGRAM_BOT_TOKEN_LIVE:
        await _send_to_bot(TELEGRAM_BOT_TOKEN_LIVE, TELEGRAM_CHAT_ID_LIVE, text)
    else:
        await _send_to_bot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)


async def send_approval_request(
    wallet_id: int,
    wallet_tag: str,
    win_rate: float,
    total: int,
    won: int,
    lost: int,
    pnl: float,
    roi: float,
    followed_since: str,
) -> None:
    """Send per-wallet go-live godkendelse til paper-bot med inline Ja/Nej knapper."""
    text = (
        f"🚀 <b>{wallet_tag} er klar til live trading!</b>\n\n"
        f"📊 <b>Performance siden {followed_since}:</b>\n"
        f"  Win rate: {win_rate:.1%}  ({won}W / {lost}L / {total} resolvede)\n"
        f"  Sim. P&amp;L: ${pnl:+.2f} USDC  (ROI {roi:+.1f}%)\n\n"
        f"Vil du flytte <b>{wallet_tag}</b> til live handel?"
    )
    await _send_to_bot(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        text,
        extra={
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Ja — gå live", "callback_data": f"go_live:{wallet_id}"},
                    {"text": "❌ Nej — bliv på paper", "callback_data": f"stay_paper:{wallet_id}"},
                ]]
            }
        },
    )


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


async def _answer_callback(callback_query_id: str, text: str = "") -> None:
    """Bekræft inline-knapklik til Telegram (fjerner 'loading' spinner)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method="answerCallbackQuery"),
                json={"callback_query_id": callback_query_id, "text": text},
            )
    except Exception:
        log.debug("answerCallbackQuery fejlede for id=%s", callback_query_id)


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

                # Håndtér inline-knapper (go_live:{wallet_id} / stay_paper:{wallet_id})
                cb = update.get("callback_query")
                if cb:
                    action = cb.get("data", "")
                    cb_id  = cb.get("id", "")

                    if action.startswith("go_live:"):
                        try:
                            wallet_id = int(action.split(":", 1)[1])
                        except (ValueError, IndexError):
                            log.warning("Ugyldigt go_live callback: %r", action)
                            continue
                        async with acquire() as conn:
                            await conn.execute(
                                "UPDATE followed_wallets SET mode = 'live' WHERE wallet_id = $1",
                                wallet_id,
                            )
                            row = await conn.fetchrow(
                                """
                                SELECT COALESCE(w.label,
                                    LEFT(w.address,6)||'…'||RIGHT(w.address,4)) AS tag
                                FROM wallets w WHERE id = $1
                                """,
                                wallet_id,
                            )
                        tag = row["tag"] if row else str(wallet_id)
                        log.info("🚀 Go-live godkendt for wallet_id=%d (%s)", wallet_id, tag)
                        await _answer_callback(cb_id, "✅ Go live aktiveret!")
                        await send_telegram(
                            f"✅ <b>{tag} er nu live!</b>\n"
                            f"Fremtidige trades sendes til live-botten."
                        )

                    elif action.startswith("stay_paper:"):
                        try:
                            wallet_id = int(action.split(":", 1)[1])
                        except (ValueError, IndexError):
                            wallet_id = 0
                        log.info("📄 Stay paper valgt for wallet_id=%d", wallet_id)
                        await _answer_callback(cb_id, "📄 Forbliver på paper")
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


async def check_go_live_gate(conn: asyncpg.Connection) -> list[dict]:
    """Returnerer liste af wallets der er klar til live: >= 20 resolvede + >= 60% WR.

    Tjekker KUN wallets der stadig er på mode='paper' (ikke allerede live).
    Returnerer tom liste hvis ingen wallets er klar.
    """
    rows = await conn.fetch(
        """
        SELECT
            w.id                                            AS wallet_id,
            COALESCE(w.label,
                LEFT(w.address,6)||'…'||RIGHT(w.address,4)) AS wallet_tag,
            COUNT(*) FILTER (WHERE co.won IS NOT NULL)      AS total,
            COUNT(*) FILTER (WHERE co.won = true)           AS won,
            COUNT(*) FILTER (WHERE co.won = false)          AS lost,
            COALESCE(SUM(co.pnl_usdc) FILTER (WHERE co.won IS NOT NULL), 0) AS pnl,
            COALESCE(SUM(co.size_filled) FILTER (WHERE co.won IS NOT NULL), 0) AS invested,
            MIN(fw.followed_at)::date::text                 AS followed_since
        FROM copy_orders co
        JOIN wallets w          ON w.id = co.source_wallet_id
        JOIN followed_wallets fw ON fw.wallet_id = w.id
        WHERE fw.unfollowed_at IS NULL
          AND fw.mode = 'paper'
        GROUP BY w.id, w.label, w.address
        HAVING
            COUNT(*) FILTER (WHERE co.won IS NOT NULL) >= 20
        """
    )
    ready = []
    for r in rows:
        total = int(r["total"])
        won   = int(r["won"])
        if total == 0:
            continue
        win_rate = won / total
        if win_rate >= 0.60:
            invested = float(r["invested"] or 0)
            pnl      = float(r["pnl"] or 0)
            roi      = (pnl / invested * 100) if invested > 0 else 0
            ready.append({
                "wallet_id":     r["wallet_id"],
                "wallet_tag":    r["wallet_tag"],
                "win_rate":      win_rate,
                "total":         total,
                "won":           won,
                "lost":          int(r["lost"]),
                "pnl":           pnl,
                "roi":           roi,
                "followed_since": r["followed_since"],
            })
    return ready
