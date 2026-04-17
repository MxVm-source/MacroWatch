# bot/modules/srwatch.py
"""
S&RWatch — Support & Resistance Monitor (Daily / Weekly / Monthly)

Computes pivot points from daily, weekly, and monthly candles.
Three signal types:
  - APPROACHING — price within 0.5% of level (heads up, once per 24h per level)
  - HIT         — price within 0.1% of level (potential entry, cooldown 4h)
  - BREAKOUT    — 4H candle CLOSED beyond the level (invalidates reversal bias)

Anti-spam:
  - Approaching: 24h cooldown per level
  - Hit: 4h cooldown per level
  - Breakout: fires once per level per direction

Polls every 15 minutes.
Commands: /sr, /sr_diag
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text
from bot.datafeed_bitget import BITGET_BASE_URL, BITGET_PRODUCT_TYPE

log = logging.getLogger("srwatch")

SYMBOL = "BTCUSDT"
TICKER = "BTC"

APPROACH_PCT        = 0.5
HIT_PCT             = 0.1
APPROACH_COOLDOWN_H = 24
HIT_COOLDOWN_H      = 4

STATE = {
    "last_check":     None,
    "daily_levels":   {},
    "weekly_levels":  {},
    "monthly_levels": {},
    "last_price":     None,
    "last_4h_close":  None,
    "approaching":    {},
    "hit":            {},
    "breakout":       {},
    "breakout_time":  {},  # { key: datetime } — silence level for 24h after breakout
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
    daily = _fetch_candles("1D", 3)
    if len(daily) >= 2:
        STATE["daily_levels"] = _compute_pivots(daily[-2], "D")

    weekly = _fetch_candles("1W", 3)
    if len(weekly) >= 2:
        STATE["weekly_levels"] = _compute_pivots(weekly[-2], "W")

    monthly = _fetch_candles("1M", 3)
    if len(monthly) >= 2:
        STATE["monthly_levels"] = _compute_pivots(monthly[-2], "M")

    log.info(f"S&RWatch: levels refreshed — D:{len(STATE['daily_levels'])} W:{len(STATE['weekly_levels'])} M:{len(STATE['monthly_levels'])}")


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


def _approach_ok(key):
    last = STATE["approaching"].get(key)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=APPROACH_COOLDOWN_H)


def _hit_ok(key):
    last = STATE["hit"].get(key)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=HIT_COOLDOWN_H)


def _breakout_ok(key, direction):
    return STATE["breakout"].get(key) != direction


def _send_signal(signal_type, key, level, price, prev_close=None):
    now   = datetime.now(timezone.utc)
    label = _level_label(key)
    is_res = level > price
    color  = "🔴" if is_res else "🟢"
    dist   = abs(level - price) / level * 100
    sign   = "+" if is_res else "-"

    if signal_type == "APPROACHING":
        header = f"{color} *{'RESISTANCE' if is_res else 'SUPPORT'} APPROACHING*"
        detail = (f"_Price closing in on {label}. Get ready._\n"
                  f"_Watch for reaction — reversal or breakout._ ⚡")
    elif signal_type == "HIT":
        header = f"{color} *{'RESISTANCE' if is_res else 'SUPPORT'} HIT*"
        detail = (f"_Price is testing {label}. Potential reversal zone._\n"
                  f"_Watch for rejection candle or volume spike._ 🎯")
    else:
        up = price > level
        color  = "🟢" if up else "🔴"
        header = f"{color} *BREAKOUT {'ABOVE' if up else 'BELOW'} {label.upper()}*"
        detail = (
            f"_4H candle closed above {label}._\n_Reversal bias invalidated — momentum trade in play._ 🚀"
            if up else
            f"_4H candle closed below {label}._\n_Support broken — next level down in play._ 📉"
        )
        dist = abs(price - level) / level * 100
        sign = "+" if up else "-"

    lines = [
        f"📐 *S&RWatch — {TICKER}*",
        header,
        "",
        f"Level:    `${level:,.2f}`  ({label})",
        f"Price:    `${price:,.2f}`",
        f"Distance: `{sign}{dist:.2f}%`",
        "",
        detail,
        "",
        f"_Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}_",
    ]
    send_text("\n".join(lines))
    log.info(f"S&RWatch: {signal_type} — {key} @ ${level:,.2f} (price ${price:,.2f})")


def _breakout_silenced(key: str) -> bool:
    """Returns True if level was broken out recently — silence for 24h."""
    t = STATE["breakout_time"].get(key)
    if not t:
        return False
    return datetime.now(timezone.utc) - t < timedelta(hours=24)


def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    if not STATE["daily_levels"]:
        refresh_levels()

    # Refresh daily at 00:05 UTC
    if now.hour == 0 and now.minute < 20:
        refresh_levels()

    # Refresh weekly on Monday
    if now.weekday() == 0 and now.hour == 0 and now.minute < 20:
        refresh_levels()

    # Refresh monthly on 1st
    if now.day == 1 and now.hour == 0 and now.minute < 20:
        refresh_levels()

    candles_4h = _fetch_candles("4H", 3)
    if not candles_4h:
        return

    price      = float(candles_4h[-1][4])
    prev_close = float(candles_4h[-2][4])
    STATE["last_price"]    = price
    STATE["last_4h_close"] = prev_close

    levels = _all_levels()
    if not levels:
        return

    # ── Pass 1: check for breakouts first (highest priority, no limit) ────────
    breakout_fired = set()
    for key, level in levels.items():
        if level <= 0:
            continue
        crossed_up   = prev_close < level <= price
        crossed_down = prev_close > level >= price
        if crossed_up and _breakout_ok(key, "UP"):
            STATE["breakout"][key]      = "UP"
            STATE["breakout_time"][key] = now
            _send_signal("BREAKOUT", key, level, price, prev_close)
            breakout_fired.add(key)
        elif crossed_down and _breakout_ok(key, "DOWN"):
            STATE["breakout"][key]      = "DOWN"
            STATE["breakout_time"][key] = now
            _send_signal("BREAKOUT", key, level, price, prev_close)
            breakout_fired.add(key)

    # ── Pass 2: HIT only — single closest eligible level ────────────────────
    best_key   = None
    best_dist  = float("inf")

    for key, level in levels.items():
        if level <= 0:
            continue
        if key in breakout_fired:
            continue
        if _breakout_silenced(key):
            continue  # silenced for 24h after breakout

        dist_pct = abs(price - level) / level * 100

        if dist_pct <= HIT_PCT and _hit_ok(key):
            if dist_pct < best_dist:
                best_dist = dist_pct
                best_key  = key

    if best_key:
        level = levels[best_key]
        STATE["hit"][best_key] = now
        _send_signal("HIT", best_key, level, price)


def show_levels():
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
        "_Approaching (0.5%) · Hit (0.1%) · Breakout (4H close)_",
    ]
    send_text("\n".join(lines))


def show_diag():
    lines = ["📐 *S&RWatch Diagnostics*", ""]
    last  = STATE["last_check"]
    price = STATE["last_price"]
    silenced = sum(1 for k in STATE["breakout_time"] if _breakout_silenced(k))
    lines += [
        f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}",
        f"Price: {'${:,.2f}'.format(price) if price else '—'}",
        f"Daily levels: {len(STATE['daily_levels'])}",
        f"Weekly levels: {len(STATE['weekly_levels'])}",
        f"Monthly levels: {len(STATE['monthly_levels'])}",
        "",
        f"Approach cooldown: {APPROACH_COOLDOWN_H}h  |  Hit cooldown: {HIT_COOLDOWN_H}h",
        f"Active breakouts: {len(STATE['breakout'])}",
        f"Silenced levels (post-breakout): {silenced}",
        "",
        "_Only closest eligible level fires per poll cycle_",
    ]
    send_text("\n".join(lines))
