# bot/modules/srwatch.py
"""
S&RWatch — Support & Resistance Levels (Silent Mode)

Computes Daily / Weekly / Monthly pivot points from Bitget OHLCV.
Does NOT fire alerts. Levels are consumed by:
  - /sr command (on-demand)
  - /intel briefing
  - Weekly brief

Refresh cadence:
  - Daily pivots: once per day at 00:10 UTC
  - Weekly pivots: Monday 00:15 UTC
  - Monthly pivots: 1st of month 00:20 UTC

Commands: /sr, /sr_diag
"""

import logging
import os
from datetime import datetime, timezone

import requests

from bot.utils import send_text
from bot.datafeed_bitget import BITGET_BASE_URL, BITGET_PRODUCT_TYPE

log = logging.getLogger("srwatch")

SYMBOL = "BTCUSDT"
TICKER = "BTC"

STATE = {
    "last_refresh":   None,
    "daily_levels":   {},
    "weekly_levels":  {},
    "monthly_levels": {},
    "last_price":     None,
}


def _fetch_candles(granularity, limit):
    try:
        r = requests.get(
            f"{BITGET_BASE_URL}/api/v2/mix/market/candles",
            params={"symbol": SYMBOL, "granularity": granularity,
                    "limit": str(limit), "productType": BITGET_PRODUCT_TYPE},
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return []
        return data.get("data") or []
    except Exception as e:
        log.warning(f"Candle fetch failed {granularity}: {e}")
        return []


def _compute_pivots(candle, label):
    h  = float(candle[2])
    l  = float(candle[3])
    cl = float(candle[4])
    pp = (h + l + cl) / 3
    return {
        f"{label}_R3": round(h + 2 * (pp - l), 2),
        f"{label}_R2": round(pp + (h - l), 2),
        f"{label}_R1": round(2 * pp - l, 2),
        f"{label}_PP": round(pp, 2),
        f"{label}_S1": round(2 * pp - h, 2),
        f"{label}_S2": round(pp - (h - l), 2),
        f"{label}_S3": round(l - 2 * (h - pp), 2),
    }


def refresh_levels():
    """Recompute all pivot levels. Silent — no alerts."""
    daily = _fetch_candles("1D", 3)
    if len(daily) >= 2:
        STATE["daily_levels"] = _compute_pivots(daily[-2], "D")

    weekly = _fetch_candles("1W", 3)
    if len(weekly) >= 2:
        STATE["weekly_levels"] = _compute_pivots(weekly[-2], "W")

    monthly = _fetch_candles("1M", 3)
    if len(monthly) >= 2:
        STATE["monthly_levels"] = _compute_pivots(monthly[-2], "M")

    # Cache current price
    recent = _fetch_candles("4H", 2)
    if recent:
        STATE["last_price"] = float(recent[-1][4])

    STATE["last_refresh"] = datetime.now(timezone.utc)
    log.info(f"S&RWatch refreshed — D:{len(STATE['daily_levels'])} W:{len(STATE['weekly_levels'])} M:{len(STATE['monthly_levels'])}")


def poll_once():
    """
    Called hourly by scheduler. Refreshes levels based on time:
      - Every hour: refresh daily if hour == 0
      - Monday hour 0: refresh weekly
      - 1st of month hour 0: refresh monthly
    Otherwise just ensures levels are populated.
    """
    now = datetime.now(timezone.utc)

    # First run — populate everything
    if not STATE["daily_levels"]:
        refresh_levels()
        return

    # Periodic refresh
    should_refresh = False
    if now.hour == 0 and now.minute < 30:
        should_refresh = True  # fresh daily
    elif now.weekday() == 0 and now.hour == 0 and now.minute < 30:
        should_refresh = True  # fresh weekly
    elif now.day == 1 and now.hour == 0 and now.minute < 30:
        should_refresh = True  # fresh monthly

    if should_refresh:
        refresh_levels()
    else:
        # Just update current price for /intel and /sr
        recent = _fetch_candles("4H", 2)
        if recent:
            STATE["last_price"] = float(recent[-1][4])


def _all_levels():
    levels = {}
    levels.update(STATE["daily_levels"])
    levels.update(STATE["weekly_levels"])
    levels.update(STATE["monthly_levels"])
    return levels


def _level_label(key):
    tf_map   = {"D": "Daily", "W": "Weekly", "M": "Monthly"}
    type_map = {"PP": "Pivot", "R1": "R1", "R2": "R2", "R3": "R3",
                "S1": "S1", "S2": "S2", "S3": "S3"}
    parts = key.split("_")
    if len(parts) == 2:
        return f"{tf_map.get(parts[0], parts[0])} {type_map.get(parts[1], parts[1])}"
    return key


def show_levels():
    """On-demand /sr command — show all S/R levels."""
    now = datetime.now(timezone.utc)
    if not STATE["daily_levels"]:
        refresh_levels()

    price = STATE["last_price"]
    if not price:
        candles = _fetch_candles("4H", 2)
        price = float(candles[-1][4]) if candles else None

    lines = [
        f"📐 *S&RWatch — {TICKER}*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Price: `${price:,.2f}`" if price else "",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for tf_label, tf_levels in [
        ("📅 Daily",   STATE["daily_levels"]),
        ("📆 Weekly",  STATE["weekly_levels"]),
        ("🗓 Monthly", STATE["monthly_levels"]),
    ]:
        if not tf_levels:
            continue
        lines.append(f"\n{tf_label}")

        res = {k: v for k, v in tf_levels.items() if "_R" in k and price and v > price}
        pp  = {k: v for k, v in tf_levels.items() if "_PP" in k}
        sup = {k: v for k, v in tf_levels.items() if "_S" in k and price and v < price}

        for k, v in sorted(res.items(), key=lambda x: x[1]):
            dist = (v - price) / price * 100 if price else 0
            lines.append(f"  🔴 `${v:,.2f}`  +{dist:.1f}%  ({_level_label(k)})")
        for k, v in pp.items():
            dist = (v - price) / price * 100 if price else 0
            sign = "+" if dist >= 0 else ""
            lines.append(f"  ⚪ `${v:,.2f}`  {sign}{dist:.1f}%  (Pivot)")
        for k, v in sorted(sup.items(), key=lambda x: -x[1]):
            dist = (price - v) / price * 100 if price else 0
            lines.append(f"  🟢 `${v:,.2f}`  -{dist:.1f}%  ({_level_label(k)})")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Silent mode — levels computed for /intel and weekly brief_",
    ]
    send_text("\n".join(lines))


def show_diag():
    lines = ["📐 *S&RWatch Diagnostics*", ""]
    last  = STATE["last_refresh"]
    price = STATE["last_price"]
    lines += [
        f"Mode: SILENT (no alerts)",
        f"Last refresh: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}",
        f"Price: {'${:,.2f}'.format(price) if price else '—'}",
        f"Daily levels: {len(STATE['daily_levels'])}",
        f"Weekly levels: {len(STATE['weekly_levels'])}",
        f"Monthly levels: {len(STATE['monthly_levels'])}",
    ]
    send_text("\n".join(lines))
