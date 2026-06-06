# bot/modules/challengewatch.py
"""
ChallengeWatch — $1k → $100k LIVE Trading Challenge

Tracks Maxime's live discretionary trading performance using real closed
trade P&L from Bitget, compounded from a virtual starting capital of $1,000.

- Does NOT use account balance (avoids capital injection distortion)
- Fees already deducted in Bitget's realizedPL field
- Compounds each closed trade from CHALLENGE_START_DATE onward
- Tracks any pair (BTC/ETH/SOL/etc.) — discretionary, not systematic
- Separate from Ascent ETH (systematic strategy, runs on its own account)

Command: /challenge
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from bot.utils import send_text
from bot.datafeed_bitget import (
    _signed_request_elite,
    ELITE_API_KEY,
    _to_float,
    BITGET_PRODUCT_TYPE,
)

log = logging.getLogger("challengewatch")

# ─── Config ──────────────────────────────────────────────────────────────────

VIRTUAL_START        = float(os.getenv("CHALLENGE_START_USD",  "1000.00"))
CHALLENGE_TARGET     = float(os.getenv("CHALLENGE_TARGET_USD", "100000.00"))
CHALLENGE_START_DATE = os.getenv("CHALLENGE_START_DATE",       "2026-06-01")
CHALLENGE_MILESTONES = [2500, 5000, 10000, 25000, 50000, 100000]
# Discretionary challenge — track any pair the trader uses
# Common pairs covered; bot.utils _fetch_closed_trades can hit each symbol
SYMBOLS              = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

BITGET_URL = (
    "https://www.bitget.com/copy-trading/futures-trader-v1/"
    "bcb7467487b53c5fa395?clacCode=4Y4MLFF1"
)


# ─── Trade fetcher ────────────────────────────────────────────────────────────

def _fetch_closed_trades(start_dt: datetime, end_dt: datetime) -> list:
    """
    Fetch all filled closing orders from the Elite/discretionary account.
    The challenge tracks Maxime's live discretionary trading, NOT the
    systematic Ascent ETH bot (which lives on the main account).

    Returns list of {pnl, date, symbol}.
    """
    if not ELITE_API_KEY:
        log.warning("ChallengeWatch: ELITE_API_KEY not set — cannot fetch discretionary trades")
        return []

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    trades   = []

    for sym in SYMBOLS:
        try:
            res = _signed_request_elite(
                "GET",
                "/api/v2/mix/order/orders-history",
                params={
                    "symbol":      sym,
                    "productType": BITGET_PRODUCT_TYPE,
                    "startTime":   str(start_ms),
                    "endTime":     str(end_ms),
                    "limit":       "100",
                }
            )
            # Bitget v2 returns data.entrustedList (NOT orderList)
            orders = ((res.get("data") or {}).get("entrustedList") or [])

            for o in orders:
                # Bitget v2 fields: status (not state), totalProfits (not pnl/realizedPL)
                status     = (o.get("status") or "").lower()
                trade_side = (o.get("tradeSide") or o.get("side") or "").lower()
                pnl_raw    = (o.get("totalProfits")
                              or o.get("pnl")
                              or o.get("realizedPL")
                              or o.get("profit")
                              or "")

                if status != "filled":
                    continue
                # Only count closing trades (open trades have 0 PnL anyway, but
                # we want to be explicit and avoid double-counting)
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue
                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue
                # Skip zero-PnL (typically reduce-only fills that didn't realize)
                if pnl == 0:
                    continue

                try:
                    ctime    = int(o.get("cTime") or o.get("uTime") or 0)
                    dt       = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc)
                    date_str = dt.strftime("%b %d")
                    ts       = ctime
                except Exception:
                    date_str = "—"
                    ts       = 0

                trades.append({
                    "pnl":    pnl,
                    "date":   date_str,
                    "symbol": sym.replace("USDT", ""),
                    "ts":     ts,
                })

        except Exception as e:
            log.warning(f"Trade fetch failed for {sym}: {e}")

    # Sort chronologically by timestamp
    trades.sort(key=lambda x: x.get("ts", 0))
    return trades


# ─── Equity compounding ───────────────────────────────────────────────────────

def _compound(trades: list) -> float:
    equity = VIRTUAL_START
    for t in trades:
        equity += t["pnl"]
    return round(equity, 2)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _progress_bar(pct: float, width: int = 10) -> str:
    filled = int(min(max(pct, 0), 100) / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _calc_eta(equity: float, days_running: int) -> str:
    if days_running <= 0 or equity <= VIRTUAL_START:
        return "—"
    gain_per_day = (equity - VIRTUAL_START) / days_running
    if gain_per_day <= 0:
        return "—"
    days_remaining = (CHALLENGE_TARGET - equity) / gain_per_day
    eta_date = datetime.now(timezone.utc) + timedelta(days=days_remaining)
    return eta_date.strftime("%b %Y")


# ─── Message builder ─────────────────────────────────────────────────────────

def build_challenge() -> str:
    now = datetime.now(timezone.utc)

    # Parse start date
    try:
        start_dt     = datetime.strptime(CHALLENGE_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_running = (now - start_dt).days
        start_str    = start_dt.strftime("%b %d, %Y")
    except Exception:
        start_dt     = now
        days_running = 0
        start_str    = "—"

    # ── Pre-start mode ────────────────────────────────────────────────────────
    if days_running < 0:
        days_to_go = abs(days_running)
        lines = [
            "🚀 *OFFICIAL LAUNCH — June 1, 2026*",
            "",
            "🎯 *$1k → $100k LIVE Trading Challenge*",
            f"⏳ Live in *{days_to_go} days*",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Virtual capital:  `${VIRTUAL_START:,.0f}`",
            f"🏁 Target:           `${CHALLENGE_TARGET:,.0f}`",
            f"📈 Required:         `×{CHALLENGE_TARGET / VIRTUAL_START:,.0f}`",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🏆 *Milestones*",
            f"⬜ ${VIRTUAL_START:,.0f} — Start 🚀",
        ]
        for ms in CHALLENGE_MILESTONES:
            lines.append(f"⬜ ${ms:,.0f}")
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "*What happens June 1:*",
            "→ Maxime's live discretionary trading begins",
            "→ Every closed trade compounds into the challenge",
            "→ Real PnL. Public. Logged. Auto-updated weekly.",
            "",
            "_This is a discretionary trading challenge._",
            "_Real trades, real risk — not a backtested system._",
            "_Capital loaded. Clock starts {}._".format(start_str),
            "",
            f"🔗 [Follow on Bitget]({BITGET_URL})",
        ]
        return "\n".join(lines)

    # ── Live mode ─────────────────────────────────────────────────────────────
    try:
        trades = _fetch_closed_trades(start_dt, now)
    except Exception as e:
        log.warning(f"Trade fetch error: {e}")
        trades = []

    equity   = _compound(trades)
    gain_usd = equity - VIRTUAL_START
    gain_pct = (gain_usd / VIRTUAL_START) * 100
    progress = min(max((equity - VIRTUAL_START) / (CHALLENGE_TARGET - VIRTUAL_START) * 100, 0), 100)
    bar      = _progress_bar(progress)

    gain_sign  = "+" if gain_usd >= 0 else ""
    gain_emoji = "📈" if gain_usd >= 0 else "📉"

    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] <= 0)
    win_rate  = round(wins / len(trades) * 100) if trades else 0

    daily_avg  = gain_usd / max(days_running, 1)
    weekly_avg = daily_avg * 7
    eta        = _calc_eta(equity, max(days_running, 1))

    # Milestones
    milestone_lines = [f"✅ ${VIRTUAL_START:,.0f} — Start 🚀"]
    next_milestone  = None
    for ms in CHALLENGE_MILESTONES:
        if equity >= ms:
            milestone_lines.append(f"✅ ${ms:,.0f}")
        else:
            if next_milestone is None:
                next_milestone = ms
                milestone_lines.append(f"⬜ ${ms:,.0f}  ← next  (${ms - equity:,.0f} away)")
            else:
                milestone_lines.append(f"⬜ ${ms:,.0f}")

    lines = [
        "🎯 *$1k → $100k LIVE Trading Challenge*",
        f"📅 Day {max(days_running, 1)} — Started {start_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Equity:    `${equity:,.2f}`",
        f"{gain_emoji} Gain:      `{gain_sign}${gain_usd:,.2f}` ({gain_sign}{gain_pct:.1f}%)",
        f"🏁 Target:    `${CHALLENGE_TARGET:,.0f}`",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Progress: {progress:.2f}%",
        f"`{bar}`  ${equity:,.0f} / ${CHALLENGE_TARGET:,.0f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *Milestones*",
    ]
    lines += milestone_lines
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 *Performance*",
        f"Trades:       `{len(trades)}` ({wins}W / {losses}L)",
        f"Win rate:     `{win_rate}%`",
        f"Daily avg:    `{gain_sign}${daily_avg:,.2f}`",
        f"Weekly avg:   `{gain_sign}${weekly_avg:,.2f}`",
        f"Est. target:  `{eta}`",
        "",
        "_Live discretionary trades by Maxime. Real risk, real PnL._",
        "_Ascent ETH (systematic) runs separately — see /status._",
        "",
        f"🔗 [Follow on Bitget]({BITGET_URL})",
    ]

    return "\n".join(lines)


# ─── Entry point ─────────────────────────────────────────────────────────────

def show_challenge():
    try:
        msg = build_challenge()
        if msg:
            send_text(msg)
    except Exception as e:
        log.exception(f"ChallengeWatch failed: {e}")
        send_text(f"🎯 [Challenge] ⚠️ Error: {str(e)[:200]}")


def show_challenge_diag():
    """Diagnostic — show raw Elite trade data so we can debug filter mismatches."""
    if not ELITE_API_KEY:
        send_text("🎯 [Diag] ELITE_API_KEY not set")
        return

    try:
        now      = datetime.now(timezone.utc)
        start_dt = datetime.strptime(CHALLENGE_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(now.timestamp() * 1000)

        lines = [f"🎯 *Challenge Diag* — Elite trades since {CHALLENGE_START_DATE}", ""]

        for sym in SYMBOLS:
            res = _signed_request_elite(
                "GET",
                "/api/v2/mix/order/orders-history",
                params={
                    "symbol":      sym,
                    "productType": BITGET_PRODUCT_TYPE,
                    "startTime":   str(start_ms),
                    "endTime":     str(end_ms),
                    "limit":       "100",
                }
            )
            orders = ((res.get("data") or {}).get("entrustedList") or [])
            lines.append(f"*{sym}*: {len(orders)} raw orders")

            # Show first 3 orders fully — to inspect the field values
            for o in orders[:3]:
                status     = o.get("status") or "—"
                trade_side = o.get("tradeSide") or o.get("side") or "—"
                pnl        = (o.get("totalProfits")
                              or o.get("pnl")
                              or o.get("realizedPL")
                              or o.get("profit")
                              or "—")
                size       = o.get("size") or o.get("baseVolume") or "—"
                lines.append(f"  • status=`{status}` tradeSide=`{trade_side}` totalProfits=`{pnl}` size=`{size}`")

            lines.append("")

        send_text("\n".join(lines))
    except Exception as e:
        log.exception(f"Diag failed: {e}")
        send_text(f"🎯 [Diag] Error: {str(e)[:300]}")
