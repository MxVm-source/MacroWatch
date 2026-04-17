# bot/modules/srwatch.py
"""
S&RWatch — Support & Resistance Level Monitor

Computes key S/R levels from 4H OHLCV (Bitget public API):
  - Pivot points (classic + weekly)
  - Recent swing highs/lows (last 50 candles)
  - Psychological round numbers
  - Weekly and monthly open

Fires an alert when price enters within X% of a key level.
Cooldown per level to avoid spam.

Commands:
  /sr       — Show current S/R levels for ETH/BNB/SOL
  /sr_diag  — Show last check + alert history
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text
from bot.datafeed_bitget import BITGET_BASE_URL, BITGET_PRODUCT_TYPE

log = logging.getLogger("srwatch")

ASSETS = [
    {"symbol": "BTCUSDT", "ticker": "BTC"},
    # {"symbol": "ETHUSDT", "ticker": "ETH"},  # uncomment to add ETH
]

PROXIMITY_PCT  = 0.8   # % distance to trigger alert
COOLDOWN_MIN   = 120   # 2h per level
CANDLE_LIMIT   = 100   # 4H candles for swing detection
SWING_LOOKBACK = 5     # bars each side for swing high/low detection

STATE = {
    "last_check":  None,
    "last_alert":  {},   # { "ETHUSDT_1580": datetime }
    "levels":      {},   # { symbol: { "resistance": [...], "support": [...] } }
    "prices":      {},   # { symbol: float }
}


# ─── Candle fetch ─────────────────────────────────────────────────────────────

def _fetch_candles(symbol: str, limit: int = CANDLE_LIMIT) -> list:
    try:
        r = requests.get(
            f"{BITGET_BASE_URL}/api/v2/mix/market/candles",
            params={
                "symbol":      symbol,
                "granularity": "4H",
                "limit":       str(limit),
                "productType": BITGET_PRODUCT_TYPE,
            },
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return []
        return data.get("data") or []
    except Exception as e:
        log.warning(f"Candle fetch failed for {symbol}: {e}")
        return []


def _fetch_weekly_candles(symbol: str) -> list:
    try:
        r = requests.get(
            f"{BITGET_BASE_URL}/api/v2/mix/market/candles",
            params={
                "symbol":      symbol,
                "granularity": "1W",
                "limit":       "10",
                "productType": BITGET_PRODUCT_TYPE,
            },
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return []
        return data.get("data") or []
    except Exception as e:
        log.warning(f"Weekly candle fetch failed for {symbol}: {e}")
        return []


# ─── Level computation ────────────────────────────────────────────────────────

def _swing_highs_lows(candles: list, lookback: int = SWING_LOOKBACK) -> tuple[list, list]:
    """Find swing highs and lows with touch count."""
    highs_raw = [float(c[2]) for c in candles]
    lows_raw  = [float(c[3]) for c in candles]

    swing_highs = []
    swing_lows  = []

    for i in range(lookback, len(candles) - lookback):
        # Swing high: highest point in window
        if highs_raw[i] == max(highs_raw[i - lookback:i + lookback + 1]):
            swing_highs.append(round(highs_raw[i], 2))
        # Swing low: lowest point in window
        if lows_raw[i] == min(lows_raw[i - lookback:i + lookback + 1]):
            swing_lows.append(round(lows_raw[i], 2))

    return swing_highs, swing_lows


def _pivot_points(candles: list) -> dict:
    """Classic pivot points from last completed 4H candle."""
    if not candles:
        return {}
    # Use second-to-last candle (last completed)
    c  = candles[-2] if len(candles) >= 2 else candles[-1]
    h  = float(c[2])
    l  = float(c[3])
    cl = float(c[4])

    pp = (h + l + cl) / 3
    r1 = 2 * pp - l
    r2 = pp + (h - l)
    r3 = h + 2 * (pp - l)
    s1 = 2 * pp - h
    s2 = pp - (h - l)
    s3 = l - 2 * (h - pp)

    return {
        "pp": round(pp, 2),
        "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
    }


def _psychological_levels(price: float) -> list:
    """Round number levels near current price."""
    if price >= 1000:
        step = 100
    elif price >= 100:
        step = 10
    else:
        step = 5

    base   = int(price / step) * step
    levels = []
    for mult in range(-5, 6):
        lvl = base + mult * step
        if lvl > 0:
            levels.append(float(lvl))
    return levels


def _cluster_levels(levels: list, tolerance_pct: float = 0.5) -> list:
    """Merge levels that are within tolerance% of each other."""
    if not levels:
        return []
    sorted_lvls = sorted(set(levels))
    clusters    = []
    current     = [sorted_lvls[0]]

    for lvl in sorted_lvls[1:]:
        if abs(lvl - current[-1]) / current[-1] * 100 <= tolerance_pct:
            current.append(lvl)
        else:
            clusters.append(round(sum(current) / len(current), 2))
            current = [lvl]
    clusters.append(round(sum(current) / len(current), 2))
    return clusters


def compute_levels(symbol: str) -> dict:
    """Compute full S/R level set for a symbol."""
    candles = _fetch_candles(symbol)
    if not candles:
        return {}

    price = float(candles[-1][4])

    swing_highs, swing_lows = _swing_highs_lows(candles)
    pivots   = _pivot_points(candles)
    psych    = _psychological_levels(price)

    # Weekly open
    weekly   = _fetch_weekly_candles(symbol)
    week_open = float(weekly[-1][1]) if weekly else None
    week_high = float(weekly[-1][2]) if weekly else None
    week_low  = float(weekly[-1][3]) if weekly else None

    # Aggregate all resistance (above price) and support (below price)
    all_above = [l for l in swing_highs + psych +
                 list(pivots.values()) + ([week_high] if week_high else [])
                 if l > price * 1.001]

    all_below = [l for l in swing_lows + psych +
                 list(pivots.values()) + ([week_low] if week_low else []) +
                 ([week_open] if week_open and week_open < price else [])
                 if l < price * 0.999]

    resistance = sorted(_cluster_levels(all_above))[:5]
    support    = sorted(_cluster_levels(all_below), reverse=True)[:5]

    return {
        "price":      price,
        "resistance": resistance,
        "support":    support,
        "pivots":     pivots,
        "week_open":  week_open,
    }


# ─── Proximity alert ─────────────────────────────────────────────────────────

def _level_key(symbol: str, level: float) -> str:
    return f"{symbol}_{int(level)}"


def _cooldown_ok(key: str) -> bool:
    last = STATE["last_alert"].get(key)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


def _check_proximity(symbol: str, ticker: str, levels_data: dict) -> None:
    now   = datetime.now(timezone.utc)
    price = levels_data.get("price")
    if not price:
        return

    alerts = []

    for lvl in levels_data.get("resistance", []):
        dist_pct = (lvl - price) / price * 100
        if 0 < dist_pct <= PROXIMITY_PCT:
            key = _level_key(symbol, lvl)
            if _cooldown_ok(key):
                alerts.append({"level": lvl, "type": "RESISTANCE", "dist": dist_pct})
                STATE["last_alert"][key] = now

    for lvl in levels_data.get("support", []):
        dist_pct = (price - lvl) / price * 100
        if 0 < dist_pct <= PROXIMITY_PCT:
            key = _level_key(symbol, lvl)
            if _cooldown_ok(key):
                alerts.append({"level": lvl, "type": "SUPPORT", "dist": dist_pct})
                STATE["last_alert"][key] = now

    for alert in alerts:
        lvl_type = alert["type"]
        emoji    = "🔴" if lvl_type == "RESISTANCE" else "🟢"
        implication = (
            "Price approaching resistance. Watch for rejection or breakout."
            if lvl_type == "RESISTANCE"
            else "Price approaching support. Watch for bounce or breakdown."
        )

        lines = [
            f"📐 *S&RWatch — {ticker}*",
            f"{emoji} *{lvl_type} APPROACHING*",
            "",
            f"Level:    `${alert['level']:,.2f}`",
            f"Price:    `${price:,.2f}`",
            f"Distance: `{alert['dist']:.2f}%` away ⚠️",
            "",
            f"_{implication}_",
            "",
            f"_Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}_",
        ]
        send_text("\n".join(lines))
        log.info(f"S&RWatch: {ticker} approaching {lvl_type} at ${alert['level']:,.2f}")


# ─── Poll ─────────────────────────────────────────────────────────────────────

def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    for asset in ASSETS:
        sym    = asset["symbol"]
        ticker = asset["ticker"]
        try:
            levels = compute_levels(sym)
            if levels:
                STATE["levels"][sym]  = levels
                STATE["prices"][sym]  = levels["price"]
                _check_proximity(sym, ticker, levels)
        except Exception as e:
            log.warning(f"S&RWatch error for {sym}: {e}")


# ─── /sr command — show all levels ───────────────────────────────────────────

def show_levels():
    now   = datetime.now(timezone.utc)
    lines = [
        "📐 *S&RWatch — Key Levels*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for asset in ASSETS:
        sym    = asset["symbol"]
        ticker = asset["ticker"]

        lvls  = STATE["levels"].get(sym)
        if not lvls:
            try:
                lvls = compute_levels(sym)
                STATE["levels"][sym] = lvls
            except Exception:
                pass

        if not lvls:
            lines += ["", f"*{ticker}*: ⚠️ Data unavailable"]
            continue

        price = lvls["price"]
        res   = lvls.get("resistance", [])
        sup   = lvls.get("support", [])

        lines += ["", f"*{ticker}*  `${price:,.2f}`", ""]

        lines.append("  🔴 *Resistance:*")
        for r in res[:4]:
            dist = (r - price) / price * 100
            lines.append(f"    `${r:,.2f}`  (+{dist:.1f}%)")

        lines.append("")
        lines.append("  🟢 *Support:*")
        for s in sup[:4]:
            dist = (price - s) / price * 100
            lines.append(f"    `${s:,.2f}`  (-{dist:.1f}%)")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    lines.append("_Levels from 4H swing highs/lows + pivots + round numbers_")
    send_text("\n".join(lines))


# ─── /sr_diag ────────────────────────────────────────────────────────────────

def show_diag():
    lines = ["📐 *S&RWatch Diagnostics*", ""]
    last = STATE["last_check"]
    lines.append(f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}")
    lines.append(f"Proximity threshold: {PROXIMITY_PCT}%")
    lines.append(f"Cooldown: {COOLDOWN_MIN}min per level")
    lines.append("")
    lines.append("*Current prices:*")
    for asset in ASSETS:
        sym    = asset["symbol"]
        ticker = asset["ticker"]
        price  = STATE["prices"].get(sym)
        lines.append(f"  {ticker}: {'${:,.2f}'.format(price) if price else '—'}")
    send_text("\n".join(lines))
