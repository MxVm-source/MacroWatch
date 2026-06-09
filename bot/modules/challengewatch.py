# bot/modules/challengewatch.py
"""
ChallengeWatch — Two parallel challenges:

  1. ATRb v2 Bot Challenge ($1k -> $100k)
     - Tracks the systematic ATRb v2 bot's closed PnL on the sub-account
     - Signed with BITGET_API_KEY credentials
     - Fires to PUBLIC + PRIVATE channels (Tuesday cadence handled in main.py)

  2. TraderWatch Challenge ($1k -> $10k)
     - Tracks TraderWatch's discretionary trading on the standard/Elite account
     - Signed with ELITE_API_KEY credentials
     - Fires to PRIVATE channel only - exclusive content for active traders

Both challenges:
- Compound from a virtual $1,000 starting capital
- Use Bitget v2 orders-history endpoint with proper field mapping
- Do NOT use raw account balance (avoids capital injection distortion)

Commands:
  /challenge         -> Live challenge (backwards compat alias)
  /bot_challenge     -> ATRb v2 systematic challenge
  /live_challenge    -> TraderWatch discretionary challenge
  /challenge_diag    -> Raw API debug dump
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from bot.utils import send_text
from bot.datafeed_bitget import (
    _signed_request,
    _signed_request_elite,
    BITGET_API_KEY,
    ELITE_API_KEY,
    BITGET_PRODUCT_TYPE,
)

log = logging.getLogger("challengewatch")

# ----- Configs --------------------------------------------------------------

VIRTUAL_START = float(os.getenv("CHALLENGE_START_USD", "1000.00"))

BITGET_URL = (
    "https://www.bitget.com/copy-trading/futures-trader-v1/"
    "bcb7467487b53c5fa395?clacCode=4Y4MLFF1"
)

BOT_CONFIG = {
    "name":         "ATRb v2 Bot Challenge",
    "header":       "🤖 *$1k → $100k — ATRb v2 Bot Challenge*",
    "target":       100000.00,
    "milestones":   [2500, 5000, 10000, 25000, 50000, 100000],
    "start_date":   os.getenv("BOT_CHALLENGE_START_DATE",  "2026-06-07"),
    "symbols":      ["ETHUSDT"],
    "signer":       "main",
    "footer_lines": [
        "_Fully automated. No discretion. No emotion._",
        "_TraderWatch discretionary trading is separate — private group._",
    ],
    "follow_link":  None,        # ATRb v2 copy-trading not yet live
    "link_label":   None,
}

LIVE_CONFIG = {
    "name":         "TraderWatch Challenge",
    "header":       "🎯 *$1k → $10k — TraderWatch Challenge*",
    "target":       10000.00,
    "milestones":   [2000, 3000, 5000, 7500, 10000],
    "start_date":   os.getenv("LIVE_CHALLENGE_START_DATE", "2026-05-01"),
    "symbols":      ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "signer":       "elite",
    "footer_lines": [
        "_Live discretionary trades by TraderWatch. Real risk, real PnL._",
        "_ATRb v2 (systematic) runs separately — see_ `/bot_challenge`",
        "_Copy-trading goes live once Elite-qualified._",
    ],
    "follow_link":  None,
    "link_label":   None,
}


# ----- Trade fetcher --------------------------------------------------------

def _fetch_closed_trades(config: dict, start_dt: datetime, end_dt: datetime) -> list:
    signer  = config["signer"]
    symbols = config["symbols"]

    if signer == "main":
        if not BITGET_API_KEY:
            log.warning(f"{config['name']}: BITGET_API_KEY not set")
            return []
        sign_fn = _signed_request
    elif signer == "elite":
        if not ELITE_API_KEY:
            log.warning(f"{config['name']}: ELITE_API_KEY not set")
            return []
        sign_fn = _signed_request_elite
    else:
        log.error(f"Unknown signer: {signer}")
        return []

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    trades   = []

    for sym in symbols:
        try:
            res = sign_fn(
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

            for o in orders:
                status     = (o.get("status") or "").lower()
                trade_side = (o.get("tradeSide") or o.get("side") or "").lower()
                pnl_raw    = (o.get("totalProfits")
                              or o.get("pnl")
                              or o.get("realizedPL")
                              or o.get("profit")
                              or "")

                if status != "filled":
                    continue
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue
                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue
                if pnl == 0:
                    continue

                try:
                    ctime    = int(o.get("cTime") or o.get("uTime") or 0)
                    dt       = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc)
                    date_str = dt.strftime("%b %d")
                    ts       = ctime
                except Exception:
                    date_str = "--"
                    ts       = 0

                trades.append({
                    "pnl":    pnl,
                    "date":   date_str,
                    "symbol": sym.replace("USDT", ""),
                    "ts":     ts,
                })

        except Exception as e:
            log.warning(f"{config['name']} - trade fetch failed for {sym}: {e}")

    trades.sort(key=lambda x: x.get("ts", 0))
    return trades


def _compound(trades: list) -> float:
    equity = VIRTUAL_START
    for t in trades:
        equity += t["pnl"]
    return round(equity, 2)


def _progress_bar(pct: float, width: int = 10) -> str:
    filled = int(min(max(pct, 0), 100) / 100 * width)
    return "▓" * filled + "░" * (width - filled)


# ----- Message builder ------------------------------------------------------

def build_challenge(config: dict) -> str:
    now    = datetime.now(timezone.utc)
    target = config["target"]

    try:
        start_dt     = datetime.strptime(config["start_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_running = (now - start_dt).days
        start_str    = start_dt.strftime("%b %d, %Y")
    except Exception:
        start_dt     = now
        days_running = 0
        start_str    = "--"

    # Pre-start mode
    if days_running < 0:
        days_to_go = abs(days_running)
        lines = [
            f"🚀 *LAUNCH - {start_str}*",
            "",
            config["header"],
            f"⏳ Live in *{days_to_go} days*",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 Virtual capital:  `${VIRTUAL_START:,.0f}`",
            f"🏁 Target:           `${target:,.0f}`",
            f"📈 Required:         `×{target / VIRTUAL_START:,.0f}`",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "🏆 *Milestones*",
            f"⬜ ${VIRTUAL_START:,.0f} - Start 🚀",
        ]
        for ms in config["milestones"]:
            lines.append(f"⬜ ${ms:,.0f}")
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━"]
        lines += config["footer_lines"]
        if config["follow_link"]:
            lines += ["", f"🔗 [{config['link_label']}]({config['follow_link']})"]
        return "\n".join(lines)

    # Live mode
    try:
        trades = _fetch_closed_trades(config, start_dt, now)
    except Exception as e:
        log.warning(f"{config['name']} - fetch error: {e}")
        trades = []

    equity   = _compound(trades)
    gain_usd = equity - VIRTUAL_START
    gain_pct = (gain_usd / VIRTUAL_START) * 100
    progress = min(max((equity - VIRTUAL_START) / (target - VIRTUAL_START) * 100, 0), 100)
    bar      = _progress_bar(progress)

    gain_sign  = "+" if gain_usd >= 0 else ""
    gain_emoji = "📈" if gain_usd >= 0 else "📉"

    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] <= 0)
    win_rate  = round(wins / len(trades) * 100) if trades else 0

    weeks_running = max(days_running / 7, 1/7)  # min ~1 day to avoid div by zero
    weekly_avg    = gain_usd / weeks_running
    if equity > 0 and weeks_running > 0:
        weekly_pct = ((equity / VIRTUAL_START) ** (1 / weeks_running) - 1) * 100
    else:
        weekly_pct = 0
    avg_trade = gain_usd / len(trades) if trades else 0

    milestone_lines = [f"✅ ${VIRTUAL_START:,.0f} - Start 🚀"]
    next_milestone  = None
    for ms in config["milestones"]:
        if equity >= ms:
            milestone_lines.append(f"✅ ${ms:,.0f}")
        else:
            if next_milestone is None:
                next_milestone = ms
                milestone_lines.append(f"⬜ ${ms:,.0f}  <- next  (${ms - equity:,.0f} away)")
            else:
                milestone_lines.append(f"⬜ ${ms:,.0f}")

    lines = [
        config["header"],
        f"📅 Day {max(days_running, 1)} - Started {start_str}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 Equity:    `${equity:,.2f}`",
        f"{gain_emoji} Gain:      `{gain_sign}${gain_usd:,.2f}` ({gain_sign}{gain_pct:.1f}%)",
        f"🏁 Target:    `${target:,.0f}`",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Progress: {progress:.2f}%",
        f"`{bar}`  ${equity:,.0f} / ${target:,.0f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🏆 *Milestones*",
    ]
    lines += milestone_lines
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 *Performance*",
        f"Trades:        `{len(trades)}` ({wins}W / {losses}L)",
        f"Win rate:      `{win_rate}%`",
        f"Avg per trade: `{gain_sign}${avg_trade:,.2f}`",
        f"Weekly avg:    `{gain_sign}${weekly_avg:,.2f}`  ({gain_sign}{weekly_pct:.2f}% compounding)",
        "",
    ]
    lines += config["footer_lines"]
    if config["follow_link"]:
        lines += ["", f"🔗 [{config['link_label']}]({config['follow_link']})"]

    return "\n".join(lines)


# ----- Public + Private send helper -----------------------------------------

def _send_to_both(msg: str):
    """Send to private (CHAT_ID) and public (PUBLIC_CHAT_ID) channels."""
    send_text(msg)
    public_id = os.getenv("PUBLIC_CHAT_ID", "")
    tg_token  = os.getenv("TELEGRAM_TOKEN", "")
    if public_id and tg_token:
        try:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={
                    "chat_id":      public_id,
                    "text":         msg,
                    "parse_mode":   "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as e:
            log.warning(f"Public channel send failed: {e}")


# ----- Entry points ---------------------------------------------------------

def show_bot_challenge():
    """ATRb v2 Bot Challenge - public + private."""
    try:
        msg = build_challenge(BOT_CONFIG)
        _send_to_both(msg)
    except Exception as e:
        log.exception(f"Bot challenge failed: {e}")
        send_text(f"🤖 [Bot Challenge] ⚠️ Error: {str(e)[:200]}")


def show_live_challenge():
    """TraderWatch Challenge - private only."""
    try:
        msg = build_challenge(LIVE_CONFIG)
        log.info(f"Live challenge built — {len(msg)} chars")
        send_text(msg)
        log.info("Live challenge sent")
    except Exception as e:
        log.exception(f"Live challenge failed: {e}")
        send_text(f"🎯 [Live Challenge] ⚠️ Error: {str(e)[:200]}")


def show_challenge():
    """Legacy /challenge command - defaults to LIVE (TraderWatch discretionary)."""
    show_live_challenge()


def show_challenge_diag():
    """Diagnostic - show raw trade data from BOTH accounts for debugging."""
    now = datetime.now(timezone.utc)
    lines = ["🎯 *Challenge Diag*", ""]

    for config in (BOT_CONFIG, LIVE_CONFIG):
        signer = config["signer"]
        if signer == "main" and not BITGET_API_KEY:
            lines.append(f"*{config['name']}*: BITGET_API_KEY not set\n")
            continue
        if signer == "elite" and not ELITE_API_KEY:
            lines.append(f"*{config['name']}*: ELITE_API_KEY not set\n")
            continue

        try:
            start_dt = datetime.strptime(config["start_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms   = int(now.timestamp() * 1000)
            sign_fn  = _signed_request if signer == "main" else _signed_request_elite

            lines.append(f"*{config['name']}* - since {config['start_date']}")

            for sym in config["symbols"]:
                res = sign_fn(
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
                lines.append(f"  {sym}: {len(orders)} raw orders")

            lines.append("")
        except Exception as e:
            lines.append(f"  ⚠️ {config['name']} fetch error: {str(e)[:200]}\n")

    send_text("\n".join(lines))
