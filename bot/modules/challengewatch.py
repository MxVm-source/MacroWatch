# bot/modules/challengewatch.py
"""
ChallengeWatch — $1k → $100k Challenge Status

Command: /challenge
Shows full challenge progress, milestone tracker, pace stats, and ETA.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from bot.utils import send_text
from bot.datafeed_bitget import (
    get_elite_usdt_balance,
    ELITE_API_KEY,
    _signed_request,
    BITGET_PRODUCT_TYPE,
)

log = logging.getLogger("challengewatch")

# ─── Config ──────────────────────────────────────────────────────────────────

CHALLENGE_START_USD  = float(os.getenv("CHALLENGE_START_USD", "1000.00"))
CHALLENGE_TARGET_USD = float(os.getenv("CHALLENGE_TARGET_USD", "100000.00"))
CHALLENGE_START_DATE = os.getenv("CHALLENGE_START_DATE", "2026-03-01")  # set in Render env
CHALLENGE_MILESTONES = [2500, 5000, 10000, 25000, 50000, 100000]

BITGET_URL = "https://www.bitget.com/copy-trading/futures-trader-v1/bcb7467487b53c5fa395?clacCode=4Y4MLFF1"

# ─── Balance fetch ────────────────────────────────────────────────────────────

def _fetch_balance() -> float | None:
    try:
        if ELITE_API_KEY:
            return get_elite_usdt_balance()
        # Fallback to main account
        res = _signed_request(
            "GET", "/api/v2/mix/account/accounts",
            params={"productType": BITGET_PRODUCT_TYPE, "marginCoin": "USDT"}
        )
        accounts = res.get("data") or []
        if isinstance(accounts, dict):
            accounts = [accounts]
        for acc in accounts:
            if (acc.get("marginCoin") or acc.get("coin") or "").upper() == "USDT":
                return round(float(acc.get("usdtEquity") or acc.get("available") or 0), 2)
    except Exception as e:
        log.warning(f"Balance fetch failed: {e}")
    return None


# ─── Progress bar ────────────────────────────────────────────────────────────

def _progress_bar(pct: float, width: int = 10) -> str:
    filled = int(min(pct, 100) / 100 * width)
    return "▓" * filled + "░" * (width - filled)


# ─── ETA calculation ─────────────────────────────────────────────────────────

def _calc_eta(balance: float, days_running: int) -> str:
    if days_running <= 0 or balance <= CHALLENGE_START_USD:
        return "—"
    gain_per_day = (balance - CHALLENGE_START_USD) / days_running
    if gain_per_day <= 0:
        return "—"
    days_remaining = (CHALLENGE_TARGET_USD - balance) / gain_per_day
    eta_date = datetime.now(timezone.utc) + timedelta(days=days_remaining)
    return eta_date.strftime("%b %Y")


# ─── Message builder ─────────────────────────────────────────────────────────

def build_challenge() -> str:
    now = datetime.now(timezone.utc)

    # Days running
    try:
        start_dt   = datetime.strptime(CHALLENGE_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_running = max(1, (now - start_dt).days)
        start_str  = start_dt.strftime("%b %d, %Y")
    except Exception:
        days_running = 1
        start_str  = "—"

    balance = _fetch_balance()
    if balance is None:
        send_text("🎯 [Challenge] ⚠️ Could not fetch balance — check API credentials.")
        return ""

    gain_usd = balance - CHALLENGE_START_USD
    gain_pct = (gain_usd / CHALLENGE_START_USD) * 100
    progress = min((balance - CHALLENGE_START_USD) / (CHALLENGE_TARGET_USD - CHALLENGE_START_USD) * 100, 100)
    bar      = _progress_bar(progress)

    gain_sign = "+" if gain_usd >= 0 else ""
    gain_emoji = "📈" if gain_usd >= 0 else "📉"

    # Weekly avg (based on daily avg × 7)
    daily_avg  = gain_usd / days_running
    weekly_avg = daily_avg * 7

    # ETA
    eta = _calc_eta(balance, days_running)

    # Milestones
    milestone_lines = [f"✅ ${CHALLENGE_START_USD:,.0f} — Start 🚀"]
    next_milestone  = None
    for ms in CHALLENGE_MILESTONES:
        if balance >= ms:
            milestone_lines.append(f"✅ ${ms:,.0f}")
        else:
            if next_milestone is None:
                next_milestone = ms
                milestone_lines.append(f"⬜ ${ms:,.0f}  ← next  (${ms - balance:,.0f} away)")
            else:
                milestone_lines.append(f"⬜ ${ms:,.0f}")

    lines = [
        "🎯 *$1k → $100k Challenge*",
        f"📅 Day {days_running} — Started {start_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Balance:   `${balance:,.2f}`",
        f"{gain_emoji} Gain:      `{gain_sign}${gain_usd:,.2f}` ({gain_sign}{gain_pct:.1f}%)",
        f"🏁 Target:    `${CHALLENGE_TARGET_USD:,.0f}`",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Progress: {progress:.1f}%",
        f"`{bar}`  ${balance:,.0f} / ${CHALLENGE_TARGET_USD:,.0f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *Milestones*",
    ]
    lines += milestone_lines
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 *At current pace*",
        f"Daily avg:    `{gain_sign}${daily_avg:,.2f}`",
        f"Weekly avg:   `{gain_sign}${weekly_avg:,.2f}`",
        f"Est. target:  `{eta}`",
        "",
        f"🔗 [Copy on Bitget]({BITGET_URL})",
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
