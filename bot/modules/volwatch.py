# bot/modules/volwatch.py
"""
VolWatch — Weekly Volatility Scanner

Fires every Monday at 08:00 UTC (scheduled in main.py).
Scans top 250 coins via CoinGecko, ranks Bitget perps by 30-day ATR%.
Posts a formatted report to the MacroWatch private group.

Logic:
  - 30d ATR% = structural volatility (not noisy 24h range)
  - GREEN  (> 4.0%) = hot, high edge for scalping strategy
  - YELLOW (2.5–4.0%) = acceptable, monitor
  - RED    (< 2.5%) = cooling, consider rotating out

Rotation rules:
  - REMOVE signal: active asset below 2.5% for 2 consecutive weeks
  - ADD signal: non-portfolio asset above 4.0% ATR%
  - No auto-swap — human reviews and updates scalper manually

Commands:
  /volwatch — trigger immediate scan
  /vol_diag — show last scan results + rotation status
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("volwatch")

# ─── Config ──────────────────────────────────────────────────────────────────

# Update this when you rotate assets in the scalper
ACTIVE_ASSETS = ["S", "INJ", "PENDLE"]
WATCH_ASSETS  = ["LIT", "IMX"]

ATR_HOT     = 4.0   # above = green, strong signal
ATR_OK      = 2.5   # between OK and HOT = yellow, monitor
ATR_COLD    = 2.5   # below = red, rotation candidate
MIN_MCAP_M  = 100   # minimum $100M market cap

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Bitget perp universe
BITGET_PERPS = {
    "BTC","ETH","BNB","SOL","XRP","DOGE","ADA","AVAX","LINK","DOT",
    "MATIC","UNI","ATOM","INJ","OP","ARB","S","NEAR","SUI","PEPE",
    "RUNE","PENDLE","TIA","JUP","MORPHO","ENA","HYPE","WIF","BONK",
    "SEI","STRK","PYTH","WLD","GMX","SNX","LTC","VET","LIT","IMX",
    "GALA","CHZ","MANA","SAND","AXS","XLM","AAVE","CRV","TRX","APT",
    "BLUR","JASMY","DCR","1INCH","COMP","BAL","SUSHI","ENJ","FLOW",
}

# ─── State ───────────────────────────────────────────────────────────────────

STATE = {
    "last_scan_utc":    None,
    "last_results":     [],       # full ranked list from last scan
    "remove_signals":   [],
    "add_signals":      [],
    "history":          [],       # last 4 weekly snapshots for trend detection
    "total_scans":      0,
}

# ─── CoinGecko helpers ───────────────────────────────────────────────────────

def _fetch_market_page(page: int) -> list:
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order":       "market_cap_desc",
                "per_page":    125,
                "page":        page,
                "sparkline":   False,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"CoinGecko page {page} failed: {e}")
        return []


def _fetch_30d_atr(coin_id: str) -> dict | None:
    """Fetch 30 days of daily OHLCV and compute true ATR%."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": 30},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data or len(data) < 7:
            return None

        highs  = [d[2] for d in data]
        lows   = [d[3] for d in data]
        closes = [d[4] for d in data]

        trs = []
        for i in range(1, len(data)):
            if closes[i - 1] > 0:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i]  - closes[i - 1]),
                )
                trs.append(tr / closes[i - 1] * 100)

        if not trs:
            return None

        atr_30d = sum(trs) / len(trs)
        atr_7d  = sum(trs[-7:]) / min(7, len(trs[-7:]))

        # Trend: last 7d vs prior 7d
        if len(trs) >= 14:
            recent = sum(trs[-7:])  / 7
            prior  = sum(trs[-14:-7]) / 7
            if   recent > prior * 1.25: trend = "RISING 📈"
            elif recent < prior * 0.75: trend = "FALLING 📉"
            else:                       trend = "STABLE ➡️"
        else:
            trend = "NEW 🆕"

        return {
            "atr_30d": round(atr_30d, 2),
            "atr_7d":  round(atr_7d, 2),
            "trend":   trend,
        }
    except Exception as e:
        log.warning(f"ATR fetch failed for {coin_id}: {e}")
        return None


# ─── Rotation helpers ─────────────────────────────────────────────────────────

def _was_cold_last_week(symbol: str) -> bool:
    """Check if asset was below ATR_COLD threshold last week."""
    if not STATE["history"]:
        return False
    last_run = STATE["history"][-1]
    prev = next((a for a in last_run if a["symbol"] == symbol), None)
    return prev is not None and prev.get("atr_30d", 99) < ATR_COLD


# ─── Main scan ───────────────────────────────────────────────────────────────

def run_scan() -> dict:
    """
    Full volatility scan. Returns dict with results and rotation signals.
    Typically takes 2-3 minutes due to CoinGecko rate limits.
    """
    log.info("VolWatch: starting weekly scan...")
    now = datetime.now(timezone.utc)

    # Fetch market data
    all_coins = []
    for page in [1, 2]:
        all_coins.extend(_fetch_market_page(page))
        time.sleep(1.5)

    if not all_coins:
        log.error("VolWatch: no market data — aborting scan")
        return {}

    # Filter to Bitget perps with sufficient market cap
    candidates = [
        c for c in all_coins
        if c.get("symbol", "").upper() in BITGET_PERPS
        and (c.get("market_cap") or 0) >= MIN_MCAP_M * 1e6
    ]

    log.info(f"VolWatch: scanning {len(candidates)} Bitget assets...")

    results = []
    for coin in candidates:
        sym  = coin["symbol"].upper()
        mcap = (coin.get("market_cap") or 0)
        h24  = coin.get("high_24h") or 0
        l24  = coin.get("low_24h")  or 1
        range_24h = (h24 - l24) / l24 * 100 if l24 > 0 else 0

        atr_data = _fetch_30d_atr(coin["id"])
        time.sleep(1.2)  # rate limit

        if atr_data:
            atr_30d = atr_data["atr_30d"]
            atr_7d  = atr_data["atr_7d"]
            trend   = atr_data["trend"]
        else:
            # Fallback estimate
            atr_30d = round(range_24h * 0.55, 2)
            atr_7d  = round(range_24h * 0.65, 2)
            trend   = "EST. ~"

        results.append({
            "symbol":       sym,
            "coin_id":      coin["id"],
            "atr_30d":      atr_30d,
            "atr_7d":       atr_7d,
            "trend":        trend,
            "mcap_m":       round(mcap / 1e6),
            "in_portfolio": sym in ACTIVE_ASSETS,
            "in_watch":     sym in WATCH_ASSETS,
        })

    results.sort(key=lambda x: -x["atr_30d"])

    # Rotation signals
    remove_signals = []
    add_signals    = []

    for r in results:
        sym = r["symbol"]
        if r["in_portfolio"] and r["atr_30d"] < ATR_COLD:
            remove_signals.append({
                **r,
                "consecutive": _was_cold_last_week(sym),
            })
        elif not r["in_portfolio"] and r["atr_30d"] >= ATR_HOT:
            add_signals.append(r)

    # Update state
    STATE["last_scan_utc"]  = now
    STATE["last_results"]   = results
    STATE["remove_signals"] = remove_signals
    STATE["add_signals"]    = add_signals
    STATE["total_scans"]   += 1

    # Save snapshot to history (keep last 4 weeks)
    STATE["history"].append([
        {"symbol": r["symbol"], "atr_30d": r["atr_30d"]}
        for r in results[:30]
    ])
    if len(STATE["history"]) > 4:
        STATE["history"] = STATE["history"][-4:]

    log.info(
        f"VolWatch: scan complete — {len(results)} assets | "
        f"remove: {len(remove_signals)} | add: {len(add_signals)}"
    )
    return {
        "results":        results,
        "remove_signals": remove_signals,
        "add_signals":    add_signals,
    }


# ─── Message builder ─────────────────────────────────────────────────────────

def _build_report(scan: dict) -> str:
    results        = scan.get("results", [])
    remove_signals = scan.get("remove_signals", [])
    add_signals    = scan.get("add_signals", [])
    now            = datetime.now(timezone.utc)

    lines = []
    lines.append(f"📊 *VolWatch — Weekly Vol Scan*")
    lines.append(f"🗓 {now.strftime('%A %d %B %Y')}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔥 *Top 10 Volatile Assets*")
    lines.append("_30-day ATR% — structural vol_")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    for i, r in enumerate(results[:10]):
        if   r["atr_30d"] >= 6:   heat = "🔴"
        elif r["atr_30d"] >= ATR_HOT:  heat = "🟠"
        elif r["atr_30d"] >= ATR_COLD: heat = "🟡"
        else:                          heat = "⚪"

        tag = " 🔵 *[ACTIVE]*" if r["in_portfolio"] else \
              " 👁 *[WATCH]*"  if r["in_watch"]     else ""
        lines.append(
            f"{i+1}. {heat} *{r['symbol']}*  `{r['atr_30d']:.1f}%`  {r['trend']}{tag}"
        )

    # Portfolio health
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📈 *Portfolio Health*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    for sym in ACTIVE_ASSETS:
        r = next((x for x in results if x["symbol"] == sym), None)
        if r:
            if   r["atr_30d"] >= ATR_HOT:  status = "✅ HOT"
            elif r["atr_30d"] >= ATR_COLD: status = "🟡 OK"
            else:                           status = "⚠️ COOLING"
            lines.append(
                f"🔵 *{sym}*  `{r['atr_30d']:.1f}%` ATR  {r['trend']}  {status}"
            )
        else:
            lines.append(f"🔵 *{sym}*  ⚠️ Not found in top 250")

    # Rotation signals
    if remove_signals or add_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🔄 *Rotation Signals*")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

        if remove_signals:
            for r in remove_signals:
                if r["consecutive"]:
                    lines.append(
                        f"🔴 *{r['symbol']}* cooling 2nd week "
                        f"(`{r['atr_30d']:.1f}%` ATR)\n"
                        f"    → Consider replacing"
                    )
                else:
                    lines.append(
                        f"🟡 *{r['symbol']}* low vol 1st week "
                        f"(`{r['atr_30d']:.1f}%` ATR)\n"
                        f"    → Monitor next week"
                    )

        if add_signals:
            lines.append("")
            lines.append("🚀 *Heating up:*")
            for r in sorted(add_signals, key=lambda x: -x["atr_30d"])[:3]:
                mcap_s = f"${r['mcap_m']/1000:.1f}B" if r["mcap_m"] >= 1000 \
                         else f"${r['mcap_m']}M"
                lines.append(
                    f"✅ *{r['symbol']}*  `{r['atr_30d']:.1f}%` ATR  "
                    f"{r['trend']}  {mcap_s}"
                )
    else:
        lines.append("")
        lines.append("✅ *No rotation needed this week*")
        lines.append("_Portfolio running at full strength_")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("_30d ATR% = average daily range over 30 days._")
    lines.append(f"_Target: > {ATR_HOT:.1f}%  |  Review if < {ATR_COLD:.1f}%_")
    lines.append("_Next scan: next Monday 08:00 UTC_ 📅")

    return "\n".join(lines)


# ─── Diag ────────────────────────────────────────────────────────────────────

def show_diag():
    last = STATE["last_scan_utc"]
    results = STATE["last_results"]
    remove  = STATE["remove_signals"]
    add     = STATE["add_signals"]

    lines = ["📊 *VolWatch Diagnostics*", ""]

    if not last:
        lines.append("No scan run yet. Use /volwatch to trigger.")
        send_text("\n".join(lines))
        return

    lines.append(f"Last scan: {last.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Total scans: {STATE['total_scans']}")
    lines.append(f"Assets scanned: {len(results)}")
    lines.append(f"Remove signals: {len(remove)}")
    lines.append(f"Add signals: {len(add)}")
    lines.append("")
    lines.append("*Active portfolio ATR%:*")

    for sym in ACTIVE_ASSETS:
        r = next((x for x in results if x["symbol"] == sym), None)
        if r:
            lines.append(f"  {sym}: `{r['atr_30d']:.1f}%` {r['trend']}")
        else:
            lines.append(f"  {sym}: not found")

    if results:
        lines.append("")
        lines.append("*Top 5 right now:*")
        for r in results[:5]:
            lines.append(f"  {r['symbol']}: `{r['atr_30d']:.1f}%`")

    send_text("\n".join(lines))


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    """Called by APScheduler every Monday 08:00 UTC and via /volwatch command."""
    if os.getenv("ENABLE_VOLWATCH", "true").lower() not in ("1", "true", "yes", "on"):
        log.info("VolWatch disabled via env.")
        return

    log.info("VolWatch: running weekly scan...")

    try:
        scan = run_scan()
    except Exception as e:
        log.exception(f"VolWatch scan failed: {e}")
        send_text(f"📊 [VolWatch] ⚠️ Scan failed: {str(e)[:200]}")
        return

    if not scan.get("results"):
        send_text("📊 [VolWatch] ⚠️ No results returned — CoinGecko may be rate-limiting.")
        return

    report = _build_report(scan)
    send_text(report)
    log.info("VolWatch: report sent.")
