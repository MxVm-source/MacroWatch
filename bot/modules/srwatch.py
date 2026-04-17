# bot/modules/srwatch.py
"""
S&RWatch — Weekly & Monthly Support/Resistance Monitor

Computes Weekly and Monthly pivot points (R1/R2/S1/S2 only — major levels).
Fires alerts with strict anti-spam:
  - APPROACHING  — within 0.5% of level, 24h cooldown per level
  - HIT          — within 0.1% of level, 4h cooldown per level
  - BREAKOUT     — confirmed only after 4 consecutive 4H closes beyond the level
                   Fires once per direction, then level silenced 24h

Daily pivots intentionally excluded — too noisy for swing trading.
R3/S3 and Pivot Point also excluded — R1/R2/S1/S2 are the actionable levels.

Poll every 15 minutes (but alerts only fire when conditions met — rare).
Commands: /sr, /sr_diag
"""

import logging
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text
from bot.datafeed_bitget import BITGET_BASE_URL, BITGET_PRODUCT_TYPE

log = logging.getLogger("srwatch")

SYMBOL = "BTCUSDT"
TICKER = "BTC"

# Thresholds
APPROACH_PCT = 0.5
HIT_PCT      = 0.1

# Cooldowns
APPROACH_COOLDOWN_H = 24
HIT_COOLDOWN_H      = 4
BREAKOUT_SILENCE_H  = 24

# Breakout confirmation
BREAKOUT_CONFIRM_CANDLES = 4   # 4 consecutive 4H closes = 16h of sustained price action

# Major levels only
MAJOR_TYPES = ("R1", "R2", "S1", "S2")

STATE = {
    "last_check":     None,
    "weekly_levels":  {},
    "monthly_levels": {},
    "last_price":     None,

    "approaching":    {},   # { key: datetime }
    "hit":            {},   # { key: datetime }
    "breakout":       {},   # { key: "UP" or "DOWN" }
    "breakout_time":  {},   # { key: datetime } — 24h silence after breakout

    "initialized":    False,  # First run flag — seeds historical state silently
}


# ─── Candle fetch ─────────────────────────────────────────────────────────────

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


# ─── Pivot computation ────────────────────────────────────────────────────────

def _compute_pivots(candle, label):
    h  = float(candle[2])
    l  = float(candle[3])
    cl = float(candle[4])
    pp = (h + l + cl) / 3
    return {
        f"{label}_R2": round(pp + (h - l), 2),
        f"{label}_R1": round(2 * pp - l, 2),
        f"{label}_S1": round(2 * pp - h, 2),
        f"{label}_S2": round(pp - (h - l), 2),
    }


def refresh_levels():
    """Recompute Weekly and Monthly pivots."""
    weekly = _fetch_candles("1W", 3)
    if len(weekly) >= 2:
        STATE["weekly_levels"] = _compute_pivots(weekly[-2], "W")

    monthly = _fetch_candles("1M", 3)
    if len(monthly) >= 2:
        STATE["monthly_levels"] = _compute_pivots(monthly[-2], "M")

    log.info(f"S&RWatch levels refreshed — W:{len(STATE['weekly_levels'])} M:{len(STATE['monthly_levels'])}")


def _all_levels():
    levels = {}
    levels.update(STATE["weekly_levels"])
    levels.update(STATE["monthly_levels"])
    return levels


def _level_label(key):
    tf_map   = {"W": "Weekly", "M": "Monthly"}
    type_map = {"R1": "R1", "R2": "R2", "S1": "S1", "S2": "S2"}
    parts = key.split("_")
    if len(parts) == 2:
        return f"{tf_map.get(parts[0], parts[0])} {type_map.get(parts[1], parts[1])}"
    return key


# ─── Cooldown helpers ─────────────────────────────────────────────────────────

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
    """Breakout fires once per direction, then silenced 24h."""
    if STATE["breakout"].get(key) == direction:
        return False
    t = STATE["breakout_time"].get(key)
    if t and datetime.now(timezone.utc) - t < timedelta(hours=BREAKOUT_SILENCE_H):
        return False
    return True


def _breakout_silenced(key):
    """Silence APPROACHING/HIT for 24h after a breakout fires."""
    t = STATE["breakout_time"].get(key)
    if not t:
        return False
    return datetime.now(timezone.utc) - t < timedelta(hours=BREAKOUT_SILENCE_H)


# ─── Breakout confirmation (4 consecutive 4H closes) ──────────────────────────

def _confirm_breakout(level: float, candles_4h: list) -> str | None:
    """
    Returns 'UP' if last N 4H candles all closed ABOVE level.
    Returns 'DOWN' if last N 4H candles all closed BELOW level.
    Returns None otherwise.

    The last candle in candles_4h is the current (unclosed) one — skip it.
    We look at the last N COMPLETED candles.
    """
    if len(candles_4h) < BREAKOUT_CONFIRM_CANDLES + 1:
        return None

    # Last completed candles (exclude current unclosed)
    completed = candles_4h[-(BREAKOUT_CONFIRM_CANDLES + 1):-1]
    closes = [float(c[4]) for c in completed]

    if all(c > level for c in closes):
        return "UP"
    if all(c < level for c in closes):
        return "DOWN"
    return None


# ─── Alert sender ─────────────────────────────────────────────────────────────

def _send_signal(signal_type, key, level, price):
    now    = datetime.now(timezone.utc)
    label  = _level_label(key)
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
    else:  # BREAKOUT (confirmed)
        up = price > level
        color  = "🟢" if up else "🔴"
        direction = "ABOVE" if up else "BELOW"
        header = f"{color} *CONFIRMED BREAKOUT {direction} {label.upper()}*"
        detail = (
            f"_{BREAKOUT_CONFIRM_CANDLES} consecutive 4H candles closed above {label}._\n"
            f"_Structural break confirmed — momentum trade in play._ 🚀"
            if up else
            f"_{BREAKOUT_CONFIRM_CANDLES} consecutive 4H candles closed below {label}._\n"
            f"_Structural break confirmed — next level down in play._ 📉"
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


# ─── Poll ─────────────────────────────────────────────────────────────────────

def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    # First run: populate levels
    if not STATE["weekly_levels"]:
        refresh_levels()

    # Refresh weekly on Monday 00:00–00:30 UTC
    if now.weekday() == 0 and now.hour == 0 and now.minute < 30:
        refresh_levels()
    # Refresh monthly on 1st 00:00–00:30 UTC
    if now.day == 1 and now.hour == 0 and now.minute < 30:
        refresh_levels()

    # Fetch recent 4H candles for price + breakout confirmation
    candles_4h = _fetch_candles("4H", BREAKOUT_CONFIRM_CANDLES + 5)
    if not candles_4h:
        return

    price = float(candles_4h[-1][4])
    STATE["last_price"] = price

    levels = _all_levels()
    if not levels:
        return

    # Filter to major levels only (R1/R2/S1/S2)
    major_levels = {k: v for k, v in levels.items() if any(k.endswith(f"_{t}") for t in MAJOR_TYPES)}
    if not major_levels:
        return

    # ── First run: seed historical state silently (no alerts fired) ───────────
    if not STATE["initialized"]:
        seeded = 0
        for key, level in major_levels.items():
            if level <= 0:
                continue
            direction = _confirm_breakout(level, candles_4h)
            if direction:
                # Mark as already broken — silence for 24h
                STATE["breakout"][key]      = direction
                STATE["breakout_time"][key] = now
                seeded += 1
        STATE["initialized"] = True
        log.info(f"S&RWatch: initialized — silently seeded {seeded} historical breakouts")
        return

    # ── Pass 1: confirmed breakouts (sustained 4 closes) ──────────────────────
    breakout_fired = set()
    for key, level in major_levels.items():
        if level <= 0:
            continue
        direction = _confirm_breakout(level, candles_4h)
        if direction and _breakout_ok(key, direction):
            STATE["breakout"][key]      = direction
            STATE["breakout_time"][key] = now
            _send_signal("BREAKOUT", key, level, price)
            breakout_fired.add(key)

    # ── Pass 2: HIT / APPROACHING (single closest eligible level) ─────────────
    best_key   = None
    best_dist  = float("inf")
    best_type  = None

    for key, level in major_levels.items():
        if level <= 0:
            continue
        if key in breakout_fired:
            continue
        if _breakout_silenced(key):
            continue

        dist_pct = abs(price - level) / level * 100

        if dist_pct <= HIT_PCT and _hit_ok(key):
            if dist_pct < best_dist:
                best_dist = dist_pct
                best_key  = key
                best_type = "HIT"

        elif dist_pct <= APPROACH_PCT and _approach_ok(key):
            if dist_pct < best_dist:
                best_dist = dist_pct
                best_key  = key
                best_type = "APPROACHING"

    if best_key:
        level = major_levels[best_key]
        if best_type == "HIT":
            STATE["hit"][best_key] = now
        else:
            STATE["approaching"][best_key] = now
        _send_signal(best_type, best_key, level, price)


# ─── /sr command ──────────────────────────────────────────────────────────────

def show_levels():
    now = datetime.now(timezone.utc)
    if not STATE["weekly_levels"]:
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
        ("📆 Weekly",  STATE["weekly_levels"]),
        ("🗓 Monthly", STATE["monthly_levels"]),
    ]:
        if not tf_levels:
            continue

        filtered = {k: v for k, v in tf_levels.items() if any(k.endswith(f"_{t}") for t in MAJOR_TYPES)}
        if not filtered:
            continue

        lines.append(f"\n{tf_label}")

        res = {k: v for k, v in filtered.items() if "_R" in k and price and v > price}
        sup = {k: v for k, v in filtered.items() if "_S" in k and price and v < price}

        for k, v in sorted(res.items(), key=lambda x: x[1]):
            dist = (v - price) / price * 100 if price else 0
            lines.append(f"  🔴 `${v:,.2f}`  +{dist:.1f}%  ({_level_label(k)})")
        for k, v in sorted(sup.items(), key=lambda x: -x[1]):
            dist = (price - v) / price * 100 if price else 0
            lines.append(f"  🟢 `${v:,.2f}`  -{dist:.1f}%  ({_level_label(k)})")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Weekly & Monthly R1/R2/S1/S2 only — major swing levels_",
        f"_Breakout confirmation: {BREAKOUT_CONFIRM_CANDLES} × 4H closes_",
    ]
    send_text("\n".join(lines))


# ─── /sr_diag ────────────────────────────────────────────────────────────────

def show_diag():
    lines = ["📐 *S&RWatch Diagnostics*", ""]
    last  = STATE["last_check"]
    price = STATE["last_price"]
    silenced = sum(1 for k in STATE["breakout_time"] if _breakout_silenced(k))
    lines += [
        f"Mode: ALERTS ON (Weekly & Monthly R1/R2/S1/S2)",
        f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}",
        f"Price: {'${:,.2f}'.format(price) if price else '—'}",
        f"Weekly levels: {len(STATE['weekly_levels'])}",
        f"Monthly levels: {len(STATE['monthly_levels'])}",
        "",
        f"Approach: {APPROACH_PCT}%  |  Hit: {HIT_PCT}%",
        f"Approach cooldown: {APPROACH_COOLDOWN_H}h  |  Hit cooldown: {HIT_COOLDOWN_H}h",
        f"Breakout: {BREAKOUT_CONFIRM_CANDLES} × 4H closes confirmation",
        f"Breakout silence: {BREAKOUT_SILENCE_H}h after trigger",
        "",
        f"Active breakouts: {len(STATE['breakout'])}",
        f"Silenced levels: {silenced}",
    ]
    send_text("\n".join(lines))
