# bot/modules/fundingwatch.py
"""
FundingWatch — Extreme Funding Rate Monitor

Fires when funding rates cross extreme thresholds on ETH/BNB/SOL.
Extreme longs = squeeze risk. Extreme shorts = short squeeze risk.

Polls every 30 minutes. Cooldown 4h per asset to avoid spam.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("fundingwatch")

BITGET_BASE    = "https://api.bitget.com"
PRODUCT_TYPE   = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

ASSETS = ["ETHUSDT", "BNBUSDT", "SOLUSDT"]
TICKERS = {"ETHUSDT": "ETH", "BNBUSDT": "BNB", "SOLUSDT": "SOL"}

# Thresholds
FUNDING_EXTREME_LONG  =  0.10   # % — above = overleveraged longs
FUNDING_EXTREME_SHORT = -0.05   # % — below = overleveraged shorts
FUNDING_WARN_LONG     =  0.06   # % — elevated but not extreme
FUNDING_WARN_SHORT    = -0.03   # % — elevated short bias

COOLDOWN_MIN = 240  # 4 hours per asset

STATE = {
    "last_alert": {},   # { symbol: datetime }
    "last_rates": {},   # { symbol: float }
    "last_check": None,
}


def _fetch_funding_rate(symbol: str) -> float | None:
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/current-fund-rate",
            params={"symbol": symbol, "productType": PRODUCT_TYPE},
            timeout=6,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None
        d = data.get("data") or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        val = d.get("fundingRate") or d.get("currentFundingRate")
        return round(float(val) * 100, 4) if val is not None else None
    except Exception as e:
        log.warning(f"Funding fetch failed for {symbol}: {e}")
        return None


def _cooldown_ok(symbol: str) -> bool:
    last = STATE["last_alert"].get(symbol)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


def _classify_rate(rate: float) -> tuple[str, str]:
    """Returns (emoji, label)"""
    if rate >= FUNDING_EXTREME_LONG:
        return "🔴", "EXTREME LONG BIAS"
    elif rate >= FUNDING_WARN_LONG:
        return "🟠", "ELEVATED LONG BIAS"
    elif rate <= FUNDING_EXTREME_SHORT:
        return "🔵", "EXTREME SHORT BIAS"
    elif rate <= FUNDING_WARN_SHORT:
        return "🟡", "ELEVATED SHORT BIAS"
    else:
        return "⚪", "NEUTRAL"


def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    rates = {}
    for sym in ASSETS:
        r = _fetch_funding_rate(sym)
        if r is not None:
            rates[sym] = r
            STATE["last_rates"][sym] = r

    if not rates:
        log.debug("FundingWatch: no data")
        return

    # Check for extreme conditions across all assets
    extreme = []
    for sym, rate in rates.items():
        emoji, label = _classify_rate(rate)
        if "EXTREME" in label and _cooldown_ok(sym):
            extreme.append((sym, rate, emoji, label))

    if not extreme:
        return

    # Build alert
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = ["💸 *FundingWatch Alert*", ""]

    # Determine dominant bias
    long_bias  = sum(1 for _, r, _, _ in extreme if r > 0)
    short_bias = sum(1 for _, r, _, _ in extreme if r < 0)

    if long_bias > short_bias:
        lines.append("🔴 *EXTREME LONG BIAS DETECTED*")
        lines.append("")
    elif short_bias > long_bias:
        lines.append("🔵 *EXTREME SHORT BIAS DETECTED*")
        lines.append("")

    # All asset rates
    for sym in ASSETS:
        rate = rates.get(sym)
        ticker = TICKERS[sym]
        if rate is not None:
            e, _ = _classify_rate(rate)
            sign = "+" if rate >= 0 else ""
            lines.append(f"{e} *{ticker}*: `{sign}{rate:.4f}%`")

    lines.append("")

    # Implication
    if long_bias >= 2:
        lines.append("_When everyone's long, who's left to buy?_")
        lines.append("_Squeeze incoming. Stay sharp._ ⚡")
    elif short_bias >= 2:
        lines.append("_Heavy short positioning — short squeeze risk elevated._")
        lines.append("_Any bounce could accelerate fast._ ⚡")
    else:
        lines.append("_Mixed bias — monitor closely._")

    lines.append("")
    lines.append(f"_Time (UTC): {now_str}_")

    send_text("\n".join(lines))

    # Mark all extreme assets as alerted
    for sym, _, _, _ in extreme:
        STATE["last_alert"][sym] = now

    log.info(f"FundingWatch: alert fired — {[s for s,_,_,_ in extreme]}")


def show_diag():
    lines = ["💸 *FundingWatch Diagnostics*", ""]
    last = STATE["last_check"]
    lines.append(f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}")
    lines.append("")
    lines.append("*Current rates:*")
    for sym in ASSETS:
        rate = STATE["last_rates"].get(sym)
        ticker = TICKERS[sym]
        if rate is not None:
            e, label = _classify_rate(rate)
            sign = "+" if rate >= 0 else ""
            lines.append(f"  {e} {ticker}: `{sign}{rate:.4f}%` — {label}")
        else:
            lines.append(f"  ⚪ {ticker}: —")
    lines.append("")
    lines.append("*Last alerts:*")
    for sym in ASSETS:
        last_alert = STATE["last_alert"].get(sym)
        ticker = TICKERS[sym]
        lines.append(f"  {ticker}: {last_alert.strftime('%H:%M UTC') if last_alert else 'None'}")
    send_text("\n".join(lines))
