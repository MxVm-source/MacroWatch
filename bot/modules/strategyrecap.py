# bot/modules/strategyrecap.py
"""
Strategy Recap — Friday 09:00 UTC

Weekly trading recap fired to both private group and public channel.
Covers:
  - Trades closed this week (count, wins, losses, net PnL)
  - Open positions still running
  - Why no trades if quiet week
  - Current strategy positioning + market stance
  - Account balance snapshot

Command: /recap — on-demand trigger
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text
from bot.datafeed_bitget import (
    _signed_request, _to_float, _position_is_open,
    BITGET_PRODUCT_TYPE, BITGET_API_KEY, BITGET_BASE_URL,
)

log = logging.getLogger("strategyrecap")

PUBLIC_CHAT_ID = os.getenv("PUBLIC_CHAT_ID", "")

ASCENT_SYMBOLS = ["ETHUSDT"]


# ─── Balance fetch (elite preferred) ──────────────────────────────────────────

def _fetch_balance() -> float | None:
    """ATRb v2 bot recap — always reads the sub-account (BITGET_API_KEY),
    never Elite. This recap reports the systematic bot's balance only."""
    try:
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


# ─── Asset 7D change from Bitget ──────────────────────────────────────────────

def _fetch_asset_weekly(symbol: str) -> float | None:
    try:
        r = requests.get(
            f"{BITGET_BASE_URL}/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": "1D",
                    "limit": "8", "productType": BITGET_PRODUCT_TYPE},
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None
        candles = data.get("data") or []
        if len(candles) < 7:
            return None
        open_price  = float(candles[-7][1])
        close_price = float(candles[-1][4])
        if open_price > 0:
            return round((close_price - open_price) / open_price * 100, 2)
    except Exception as e:
        log.warning(f"Asset weekly fetch failed for {symbol}: {e}")
    return None


# ─── Closed trades this week ──────────────────────────────────────────────────

def _fetch_closed_trades() -> list:
    """Pull closed trades from the past 7 days across all Ascent symbols."""
    if not BITGET_API_KEY:
        return []

    now         = datetime.now(timezone.utc)
    week_ago    = now - timedelta(days=7)
    start_ms    = int(week_ago.timestamp() * 1000)
    end_ms      = int(now.timestamp() * 1000)

    all_closed = []
    for sym in ASCENT_SYMBOLS:
        try:
            res = _signed_request(
                "GET", "/api/v2/mix/order/orders-history",
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
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue

                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue
                if pnl == 0:
                    continue

                try:
                    ctime = int(o.get("cTime") or o.get("uTime") or 0)
                    date  = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc).strftime("%b %d")
                except Exception:
                    date = "—"

                side = (o.get("posSide") or o.get("holdSide") or trade_side or "").upper()
                all_closed.append({"symbol": sym, "pnl": pnl, "date": date, "side": side})
        except Exception as e:
            log.warning(f"Closed trades fetch failed for {sym}: {e}")

    return all_closed


# ─── Open positions ───────────────────────────────────────────────────────────

def _fetch_open_positions() -> list:
    """Check for open positions on Ascent symbols."""
    if not BITGET_API_KEY:
        return []

    open_positions = []
    try:
        res = _signed_request(
            "GET", "/api/v2/mix/position/all-position",
            params={"productType": BITGET_PRODUCT_TYPE, "marginCoin": "USDT"},
        )
        positions = res.get("data") or []
        if isinstance(positions, dict):
            positions = [positions]

        for p in positions:
            if not isinstance(p, dict):
                continue
            sym = (p.get("symbol") or "").upper()
            if sym not in ASCENT_SYMBOLS:
                continue
            if not _position_is_open(p):
                continue
            open_positions.append({
                "symbol": sym,
                "side":   (p.get("holdSide") or "").upper(),
                "entry":  _to_float(p.get("openPriceAvg") or p.get("openPrice")),
                "size":   _to_float(p.get("total") or p.get("available")),
                "upnl":   _to_float(p.get("unrealizedPL") or p.get("upl")),
                "lev":    p.get("leverage", "?"),
            })
    except Exception as e:
        log.warning(f"Open positions fetch failed: {e}")

    return open_positions


# ─── Build message ────────────────────────────────────────────────────────────

def build_recap() -> str:
    now        = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end   = now.strftime("%b %d, %Y")

    # Fetch all data
    balance = _fetch_balance()
    closed  = _fetch_closed_trades()
    opens   = _fetch_open_positions()

    # Asset performance
    eth_w = _fetch_asset_weekly("ETHUSDT")

    # Trade stats
    wins       = sum(1 for t in closed if t["pnl"] > 0)
    losses     = sum(1 for t in closed if t["pnl"] <= 0)
    net_pnl    = sum(t["pnl"] for t in closed)
    trade_count = len(closed)

    lines = [
        "🤖 *Infinex Capital — ATRb v2 Weekly Recap*",
        "_Intelligence provided by MacroWatch 🧠_",
        f"📅 {week_start} → {week_end}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Balance
    if balance is not None:
        lines.append(f"💰 *Balance:* `${balance:,.2f}`")
        lines.append("")

    # Trades section
    lines.append("📊 *Trading Activity*")
    lines.append("")

    if trade_count == 0:
        lines.append("_No trades this week._ ⚪")
        lines.append("")
        lines.append("The strategy only fires when volatility + momentum align.")
        lines.append("No setup = no trade. Protecting capital is the first rule.")
    else:
        net_sign  = "+" if net_pnl >= 0 else ""
        net_emoji = "🟢" if net_pnl >= 0 else "🔴"
        lines.append(f"Trades closed: *{trade_count}*  ({wins}W / {losses}L)")
        lines.append(f"Net PnL: {net_emoji} `{net_sign}${abs(net_pnl):.2f}`")
        lines.append("")

        # Individual trades
        for t in closed[:6]:
            e    = "🟢" if t["pnl"] >= 0 else "🔴"
            sign = "+" if t["pnl"] >= 0 else ""
            sym_short = t["symbol"].replace("USDT", "")
            lines.append(f"  {e} {sym_short} {t['side']}: `{sign}${t['pnl']:.2f}` — {t['date']}")
        if len(closed) > 6:
            lines.append(f"  _... and {len(closed) - 6} more_")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    # Open positions
    if opens:
        lines.append("📘 *Open Positions*")
        lines.append("")
        for p in opens:
            side_e  = "📈" if p["side"] == "LONG" else "📉"
            pnl_e   = "🟢" if p["upnl"] >= 0 else "🔴"
            sign    = "+" if p["upnl"] >= 0 else ""
            sym_s   = p["symbol"].replace("USDT", "")
            lines.append(
                f"  {side_e} {sym_s} {p['side']}  Entry: `${p['entry']:,.2f}`  "
                f"uPnL: {pnl_e} `{sign}${p['upnl']:.2f}`"
            )
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    else:
        lines.append("📘 *Open Positions:* none")
        lines.append("_Strategy is flat — waiting for next setup._")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    # Market context
    lines.append("📈 *Market Context (7D)*")
    lines.append("")
    if eth_w is not None:
        e    = "📈" if eth_w >= 0 else "📉"
        sign = "+" if eth_w >= 0 else ""
        lines.append(f"  {e} ETH: `{sign}{eth_w:.2f}%`")
    else:
        lines.append("  ETH: N/A")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🤖 *ATRb v2*")
    lines.append("ETH 100%  |  4H timeframe")
    lines.append("Fully automated. No discretion. No screen time.")
    lines.append("")
    lines.append("🔗 Copy on Bitget — `/bot_challenge`")

    return "\n".join(lines)


# ─── Entry point ──────────────────────────────────────────────────────────────

def send_strategy_recap():
    try:
        msg = build_recap()
    except Exception as e:
        log.exception(f"Strategy recap build failed: {e}")
        send_text(f"🤖 [StrategyRecap] ⚠️ Build failed: {str(e)[:200]}")
        return

    # Private group
    send_text(msg)

    # Public channel
    if PUBLIC_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN', '')}/sendMessage",
                json={"chat_id": PUBLIC_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10,
            )
            log.info("Strategy recap sent to public ✅")
        except Exception as e:
            log.warning(f"Strategy recap public send failed: {e}")

    log.info("Strategy recap sent ✅")
