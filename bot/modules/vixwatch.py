# bot/modules/vixwatch.py
"""
VixWatch — CBOE VIX Fear Gauge Monitor

Polls VIX every 30 minutes. Fires alerts on threshold crosses.
Tracks direction and provides market context with ATRb strategy impact.

Thresholds:
  🟢 < 15   = Low fear, risk-on
  🟡 15–25  = Elevated, caution
  🔴 25–35  = High fear, volatility incoming
  💀 > 35   = Extreme fear / capitulation zone

Cooldown: 4h between alerts (per threshold).
Commands:
  /vix      — instant current reading
  /vix_diag — last value + state + cooldowns
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("vixwatch")

# ─── Config ──────────────────────────────────────────────────────────────────

COOLDOWN_MIN  = int(os.getenv("VIX_COOLDOWN_MIN", "240"))   # 4 hours
FINNHUB_KEY   = os.getenv("FINNHUB_API_KEY", "").strip()
BITGET_BASE   = "https://api.bitget.com"
PRODUCT_TYPE  = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

# Thresholds — ordered high to low for crossing detection
THRESHOLDS = [
    {"level": 35, "emoji": "💀", "label": "EXTREME FEAR",  "color": "💀"},
    {"level": 25, "emoji": "🔴", "label": "HIGH FEAR",     "color": "🔴"},
    {"level": 15, "emoji": "🟡", "label": "ELEVATED",      "color": "🟡"},
    {"level":  0, "emoji": "🟢", "label": "LOW FEAR",      "color": "🟢"},
]

# Context per zone
ZONE_CONTEXT = {
    "EXTREME FEAR": (
        "Options traders are pricing in a potential market crash. "
        "This level historically marks capitulation events — sharp, fast selloffs "
        "followed by violent recoveries. Maximum uncertainty in play."
    ),
    "HIGH FEAR": (
        "Options traders are pricing in serious turbulence ahead. "
        "At this level, sharp intraday swings are likely — both crypto "
        "and equities tend to sell off hard. Risk-off sentiment dominating."
    ),
    "ELEVATED": (
        "Markets are nervous but not panicking. Hedging activity is picking up "
        "and intraday volatility is increasing. Worth monitoring closely — "
        "a sustained move higher signals more stress ahead."
    ),
    "LOW FEAR": (
        "Markets are calm and complacent. Low fear historically precedes "
        "sharp moves when volatility returns. Risk-on conditions are favorable "
        "but stay alert for sudden reversals."
    ),
}

# ATRb impact per zone
ATRB_IMPACT = {
    "EXTREME FEAR": (
        "Extreme vol = extreme ATR. Entry signals may fire but risk of "
        "stop hunts and flash wicks is high. Reduce size, widen SL mentally."
    ),
    "HIGH FEAR": (
        "High volatility = wider ATR = potential entry signals forming. "
        "Monitor 4H close closely. Dynamic leverage likely at max."
    ),
    "ELEVATED": (
        "ATR expanding — strategy entering preferred operating range. "
        "Watch for confluence with MACD and OBV confirmation."
    ),
    "LOW FEAR": (
        "Low ATR environment — strategy may sit idle. "
        "No forced entries. Wait for volatility to return."
    ),
}

# ─── State ───────────────────────────────────────────────────────────────────

STATE = {
    "last_vix":         None,
    "last_vix_prev":    None,   # previous reading for change calc
    "last_zone":        None,
    "last_check_utc":   None,
    "last_alert_utc":   None,
    "last_alert_zone":  None,
    "alert_count":      0,
}

# ─── Data fetchers ────────────────────────────────────────────────────────────

def _fetch_vix() -> dict | None:
    """
    Fetch VIX from Yahoo Finance (no key needed).
    Falls back to Finnhub if Yahoo fails.
    Returns dict with current, prev_close, change, change_pct.
    """
    # Yahoo Finance — primary
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        data = r.json()
        result = data["chart"]["result"][0]
        meta   = result["meta"]
        current    = float(meta.get("regularMarketPrice") or meta.get("previousClose"))
        prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose"))
        change     = round(current - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0
        return {
            "current":    round(current, 2),
            "prev_close": round(prev_close, 2),
            "change":     change,
            "change_pct": change_pct,
        }
    except Exception as e:
        log.warning(f"VIX Yahoo fetch failed: {e}")

    # Finnhub fallback
    if FINNHUB_KEY:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "VIX", "token": FINNHUB_KEY},
                timeout=6,
            )
            d = r.json()
            current    = float(d.get("c") or 0)
            prev_close = float(d.get("pc") or current)
            change     = round(current - prev_close, 2)
            change_pct = round((change / prev_close) * 100, 2) if prev_close else 0
            if current > 0:
                return {
                    "current":    round(current, 2),
                    "prev_close": round(prev_close, 2),
                    "change":     change,
                    "change_pct": change_pct,
                }
        except Exception as e:
            log.warning(f"VIX Finnhub fallback failed: {e}")

    return None


def _fetch_crypto_changes() -> dict:
    """Fetch BTC and ETH 24h change % from Bitget."""
    result = {}
    for sym, key in [("BTCUSDT", "btc"), ("ETHUSDT", "eth")]:
        try:
            r = requests.get(
                f"{BITGET_BASE}/api/v2/mix/market/ticker",
                params={"symbol": sym, "productType": PRODUCT_TYPE},
                timeout=6,
            )
            data = r.json()
            if data.get("code") != "00000":
                continue
            tick = data.get("data") or {}
            if isinstance(tick, list):
                tick = tick[0] if tick else {}
            v = tick.get("change24h") or tick.get("priceChangePercent")
            if v:
                val = float(v) * 100 if abs(float(v)) < 1 else float(v)
                result[key] = round(val, 2)
        except Exception:
            pass
    return result


# ─── Zone helpers ─────────────────────────────────────────────────────────────

def _get_zone(vix: float) -> dict:
    for t in THRESHOLDS:
        if vix >= t["level"]:
            return t
    return THRESHOLDS[-1]


def _cooldown_ok() -> bool:
    last = STATE["last_alert_utc"]
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


# ─── Alert builder ────────────────────────────────────────────────────────────

def _build_alert(vix_data: dict, zone: dict, crossed_from: str, now: datetime) -> str:
    vix        = vix_data["current"]
    change     = vix_data["change"]
    change_pct = vix_data["change_pct"]
    label      = zone["label"]
    emoji      = zone["emoji"]

    direction  = "📈" if change >= 0 else "📉"
    sign       = "+" if change >= 0 else ""
    cross_dir  = "↑ crossed above" if change >= 0 else "↓ dropped below"

    crypto     = _fetch_crypto_changes()
    btc_str    = f"{'+' if crypto.get('btc', 0) >= 0 else ''}{crypto['btc']:.1f}%" if "btc" in crypto else "—"
    eth_str    = f"{'+' if crypto.get('eth', 0) >= 0 else ''}{crypto['eth']:.1f}%" if "eth" in crypto else "—"

    context    = ZONE_CONTEXT.get(label, "")
    impact     = ATRB_IMPACT.get(label, "")

    lines = [
        f"{emoji} *VixWatch — {label}*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"VIX: `{vix:.2f}`  {direction} {sign}{change:.2f} ({sign}{change_pct:.1f}%)",
        f"Level: {emoji} *{label}*  ({cross_dir} {zone['level']})",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📖 *What this means:*",
        context,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🤖 *ATRb Strategy impact:*",
        impact,
        "",
        f"📊 BTC 24h: `{btc_str}`  |  ETH 24h: `{eth_str}`",
    ]
    return "\n".join(lines)


def _build_reading(vix_data: dict, zone: dict, now: datetime) -> str:
    vix        = vix_data["current"]
    change     = vix_data["change"]
    change_pct = vix_data["change_pct"]
    label      = zone["label"]
    emoji      = zone["emoji"]

    direction  = "Rising 📈" if change >= 0 else "Falling 📉"
    sign       = "+" if change >= 0 else ""
    last_alert = STATE["last_alert_utc"]
    alert_str  = last_alert.strftime("%Y-%m-%d %H:%M UTC") if last_alert else "None yet"

    lines = [
        "📊 *VixWatch — Current Reading*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"VIX: `{vix:.2f}`",
        f"Level: {emoji} *{label}*",
        f"24h change: `{sign}{change:.2f}` ({sign}{change_pct:.1f}%)",
        f"Trend: {direction}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🟢 `< 15`    Low fear — risk on",
        "🟡 `15–25`  Elevated — caution",
        "🔴 `25–35`  High fear — volatility",
        "💀 `> 35`    Extreme — capitulation zone",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Last alert fired: {alert_str}",
    ]
    return "\n".join(lines)


# ─── Poll ────────────────────────────────────────────────────────────────────

def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check_utc"] = now

    vix_data = _fetch_vix()
    if not vix_data:
        log.debug("VixWatch: no data — skipping")
        return

    vix  = vix_data["current"]
    zone = _get_zone(vix)

    prev_vix  = STATE["last_vix"]
    prev_zone = STATE["last_zone"]

    STATE["last_vix_prev"] = prev_vix
    STATE["last_vix"]      = vix
    STATE["last_zone"]     = zone["label"]

    log.info(f"VixWatch: VIX {vix:.2f} [{zone['label']}]")

    # Fire alert on zone change only
    if prev_zone is None:
        # First run — seed state silently
        return

    if zone["label"] != prev_zone and _cooldown_ok():
        STATE["last_alert_utc"]  = now
        STATE["last_alert_zone"] = zone["label"]
        STATE["alert_count"]    += 1

        msg = _build_alert(vix_data, zone, prev_zone, now)
        send_text(msg)
        log.info(f"VixWatch alert fired: {prev_zone} → {zone['label']}")


# ─── Commands ────────────────────────────────────────────────────────────────

def show_vix():
    """Called by /vix command — instant current reading."""
    vix_data = _fetch_vix()
    now      = datetime.now(timezone.utc)

    if not vix_data:
        send_text("📊 [VixWatch] ⚠️ Could not fetch VIX — data source unavailable.")
        return

    zone = _get_zone(vix_data["current"])
    msg  = _build_reading(vix_data, zone, now)
    send_text(msg)

    # Update state
    STATE["last_vix"]       = vix_data["current"]
    STATE["last_zone"]      = zone["label"]
    STATE["last_check_utc"] = now


def show_diag():
    """Called by /vix_diag command."""
    last_check = STATE["last_check_utc"]
    last_alert = STATE["last_alert_utc"]
    vix        = STATE["last_vix"]
    zone       = STATE["last_zone"] or "—"

    vix_str = f"{vix:.2f}" if vix is not None else "—"

    lines = [
        "📊 *VixWatch Diagnostics*",
        "",
        f"Last check:  {last_check.strftime('%Y-%m-%d %H:%M UTC') if last_check else 'Never'}",
        f"Last VIX:    `{vix_str}`  [{zone}]",
        f"Last alert:  {last_alert.strftime('%Y-%m-%d %H:%M UTC') if last_alert else 'None yet'}",
        f"Alert zone:  {STATE.get('last_alert_zone') or '—'}",
        f"Total alerts: {STATE['alert_count']}",
        f"Cooldown:    {COOLDOWN_MIN}min",
        f"Data source: Yahoo Finance (Finnhub fallback)",
    ]
    send_text("\n".join(lines))
