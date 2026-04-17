# bot/modules/liquidationwatch.py
"""
LiquidationWatch — Large Liquidation Event Monitor

Uses Binance public forced liquidation endpoint (no API key needed).
Fires when a single liquidation event exceeds threshold on ETH/BNB/SOL.

Large liquidations = forced position closes = price acceleration.
Long liquidations push price DOWN. Short liquidations push price UP.

Polls every 2 minutes. Cooldown 15min per asset per side to avoid spam.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("liquidationwatch")

BINANCE_BASE = "https://fapi.binance.com"

ASSETS = [
    {"binance": "BTCUSDT", "ticker": "BTC"},
    # {"binance": "ETHUSDT", "ticker": "ETH"},  # uncomment to add ETH
]

# Thresholds
LIQ_LARGE_USD   = float(os.getenv("LIQ_LARGE_USD",   "3000000"))  # $3M single liq
LIQ_MASSIVE_USD = float(os.getenv("LIQ_MASSIVE_USD", "10000000")) # $10M+ = massive

COOLDOWN_MIN = 30  # per asset per side

STATE = {
    "last_check":    None,
    "last_alert":    {},   # { "ETHUSDT_LONG": datetime, ... }
    "seen_ids":      set(),  # dedup by order ID
    "stats":         {},   # { symbol: { "long_liqs": int, "short_liqs": int } }
}


def _fetch_liquidations(symbol: str) -> list:
    """Fetch recent forced liquidation orders from Binance."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/fapi/v1/allForceOrders",
            params={"symbol": symbol, "limit": 50},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception as e:
        log.warning(f"Liquidation fetch failed for {symbol}: {e}")
        return []


def _cooldown_ok(symbol: str, side: str) -> bool:
    key  = f"{symbol}_{side}"
    last = STATE["last_alert"].get(key)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    for asset in ASSETS:
        sym    = asset["binance"]
        ticker = asset["ticker"]

        liqs = _fetch_liquidations(sym)
        if not liqs:
            continue

        if sym not in STATE["stats"]:
            STATE["stats"][sym] = {"long_liqs": 0, "short_liqs": 0, "total_usd": 0}

        for liq in liqs:
            # Dedup by order ID
            order_id = liq.get("orderId") or liq.get("o", {}).get("i")
            if order_id and order_id in STATE["seen_ids"]:
                continue
            if order_id:
                STATE["seen_ids"].add(order_id)
                # Keep set from growing unbounded
                if len(STATE["seen_ids"]) > 5000:
                    STATE["seen_ids"] = set(list(STATE["seen_ids"])[-2000:])

            # Parse fields — Binance returns either flat or nested under "o"
            data     = liq.get("o") or liq
            side     = (data.get("S") or data.get("side") or "").upper()      # BUY or SELL
            qty      = float(data.get("q") or data.get("origQty") or 0)
            price    = float(data.get("ap") or data.get("averagePrice") or data.get("p") or 0)
            usd_val  = qty * price

            if usd_val < LIQ_LARGE_USD:
                continue

            # Side: SELL = long liquidated (price goes down)
            #        BUY  = short liquidated (price goes up)
            liq_side = "LONG" if side == "SELL" else "SHORT"
            key      = f"{sym}_{liq_side}"

            if not _cooldown_ok(sym, liq_side):
                continue

            # Update stats
            if liq_side == "LONG":
                STATE["stats"][sym]["long_liqs"] += 1
            else:
                STATE["stats"][sym]["short_liqs"] += 1
            STATE["stats"][sym]["total_usd"] += usd_val

            # Build alert
            usd_m    = usd_val / 1e6
            is_massive = usd_val >= LIQ_MASSIVE_USD

            if liq_side == "LONG":
                direction = "📉"
                bias      = "LONGS WRECKED"
                impact    = "Forced selling — price likely to accelerate DOWN."
                color     = "🔴"
            else:
                direction = "📈"
                bias      = "SHORTS SQUEEZED"
                impact    = "Forced buying — price likely to accelerate UP."
                color     = "🟢"

            size_label = "🚨 MASSIVE" if is_massive else "⚡ LARGE"

            lines = [
                f"🔥 *LiquidationWatch — {ticker}*",
                f"{size_label} {color} {bias}",
                "",
                f"Size:    `${usd_m:.2f}M`",
                f"Price:   `${price:,.2f}`",
                f"Side:    {direction} {liq_side} liquidated",
                "",
                f"_{impact}_",
                f"_Watch for follow-through in the next 1–2 candles._ ⚡",
                "",
                f"_Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}_",
            ]

            send_text("\n".join(lines))
            STATE["last_alert"][key] = now
            log.info(f"LiquidationWatch: {ticker} {liq_side} liq ${usd_m:.2f}M")


def show_diag():
    lines = ["🔥 *LiquidationWatch Diagnostics*", ""]
    last = STATE["last_check"]
    lines.append(f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}")
    lines.append(f"Threshold: ${LIQ_LARGE_USD/1e3:.0f}k · Massive: ${LIQ_MASSIVE_USD/1e6:.1f}M")
    lines.append(f"Cooldown: {COOLDOWN_MIN}min per asset per side")
    lines.append("")
    lines.append("*Session stats:*")
    for asset in ASSETS:
        sym    = asset["binance"]
        ticker = asset["ticker"]
        stats  = STATE["stats"].get(sym, {})
        if stats:
            total = stats.get("total_usd", 0) / 1e6
            lines.append(
                f"  {ticker}: {stats.get('long_liqs',0)} long liqs · "
                f"{stats.get('short_liqs',0)} short liqs · "
                f"${total:.1f}M total"
            )
        else:
            lines.append(f"  {ticker}: no events yet")
    lines.append("")
    lines.append("*Last alerts:*")
    for asset in ASSETS:
        sym    = asset["binance"]
        ticker = asset["ticker"]
        for side in ["LONG", "SHORT"]:
            key  = f"{sym}_{side}"
            last = STATE["last_alert"].get(key)
            if last:
                lines.append(f"  {ticker} {side}: {last.strftime('%H:%M UTC')}")
    send_text("\n".join(lines))
