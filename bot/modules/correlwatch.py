# bot/modules/correlwatch.py
"""
CorrelWatch — DXY vs BTC correlation monitor.

Fires an alert when DXY and BTC diverge sharply:
  - DXY up significantly + BTC down = bearish pressure signal
  - DXY down significantly + BTC up = potential tailwind signal
  - Both moving same direction = unusual, worth flagging

Polls every 30 minutes. Only fires when divergence exceeds threshold.
Cooldown prevents spam — max 1 alert per 4 hours.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("correlwatch")

# ─── Config ──────────────────────────────────────────────────────────────────

# Minimum 24h move to consider significant (percent)
DXY_THRESHOLD  = float(os.getenv("CORREL_DXY_THRESHOLD",  "0.4"))  # 0.4% DXY move
BTC_THRESHOLD  = float(os.getenv("CORREL_BTC_THRESHOLD",  "2.0"))  # 2.0% BTC move

# Cooldown between alerts (minutes)
COOLDOWN_MIN   = int(os.getenv("CORREL_COOLDOWN_MIN", "240"))  # 4 hours

FINNHUB_KEY    = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE   = "https://finnhub.io/api/v1"
BITGET_BASE    = "https://api.bitget.com"
PRODUCT_TYPE   = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

# ─── State ───────────────────────────────────────────────────────────────────

STATE = {
    "last_alert_utc": None,
    "last_dxy":       None,
    "last_btc":       None,
    "last_check_utc": None,
}


# ─── Data fetchers ────────────────────────────────────────────────────────────

def _fetch_dxy_change() -> float | None:
    """Fetch DXY 1D change % from Finnhub."""
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/quote",
            params={"symbol": "FOREX:USDX", "token": FINNHUB_KEY},
            timeout=6,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        dp = data.get("dp")
        return float(dp) if dp is not None else None
    except Exception as e:
        log.warning(f"DXY fetch failed: {e}")
        return None


def _fetch_btc_change() -> float | None:
    """Fetch BTC 24h change % from Bitget public ticker."""
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/ticker",
            params={"symbol": "BTCUSDT", "productType": PRODUCT_TYPE},
            timeout=6,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None
        tick = data.get("data") or {}
        if isinstance(tick, list):
            tick = tick[0] if tick else {}
        v = tick.get("change24h") or tick.get("priceChangePercent")
        return float(v) * 100 if v and abs(float(v)) < 1 else float(v) if v else None
    except Exception as e:
        log.warning(f"BTC change fetch failed: {e}")
        return None


# ─── Alert logic ─────────────────────────────────────────────────────────────

def _cooldown_ok() -> bool:
    last = STATE["last_alert_utc"]
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


def _classify(dxy_chg: float, btc_chg: float) -> dict | None:
    """
    Returns alert dict if divergence is significant, else None.
    """
    dxy_sig = abs(dxy_chg) >= DXY_THRESHOLD
    btc_sig = abs(btc_chg) >= BTC_THRESHOLD

    if not (dxy_sig and btc_sig):
        return None

    dxy_up = dxy_chg > 0
    btc_up = btc_chg > 0

    # Classic inverse correlation — DXY up, BTC down
    if dxy_up and not btc_up:
        return {
            "type":    "BEARISH PRESSURE",
            "emoji":   "🔴",
            "signal":  f"DXY +{dxy_chg:.2f}% / BTC {btc_chg:.2f}%",
            "note":    "Dollar strength weighing on crypto. Risk-off bias.",
        }

    # DXY weakening, BTC rallying — tailwind
    if not dxy_up and btc_up:
        return {
            "type":    "BULLISH TAILWIND",
            "emoji":   "🟢",
            "signal":  f"DXY {dxy_chg:.2f}% / BTC +{btc_chg:.2f}%",
            "note":    "Dollar weakness supporting risk assets. Watch for continuation.",
        }

    # Both up — unusual, BTC decorrelating
    if dxy_up and btc_up:
        return {
            "type":    "DECORRELATION",
            "emoji":   "🔵",
            "signal":  f"DXY +{dxy_chg:.2f}% / BTC +{btc_chg:.2f}%",
            "note":    "BTC rising despite dollar strength — unusual. Monitor closely.",
        }

    # Both down — risk-off across board
    if not dxy_up and not btc_up:
        return {
            "type":    "BROAD RISK-OFF",
            "emoji":   "⚠️",
            "signal":  f"DXY {dxy_chg:.2f}% / BTC {btc_chg:.2f}%",
            "note":    "Dollar and crypto both falling. Broad de-risking in play.",
        }

    return None


# ─── Poll ────────────────────────────────────────────────────────────────────

def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check_utc"] = now

    dxy_chg = _fetch_dxy_change()
    btc_chg = _fetch_btc_change()

    STATE["last_dxy"] = dxy_chg
    STATE["last_btc"] = btc_chg

    if dxy_chg is None or btc_chg is None:
        log.debug("CorrelWatch: missing data — skipping")
        return

    log.info(f"CorrelWatch: DXY {dxy_chg:+.2f}% / BTC {btc_chg:+.2f}%")

    alert = _classify(dxy_chg, btc_chg)
    if not alert:
        return

    if not _cooldown_ok():
        log.debug("CorrelWatch: cooldown active — skipping alert")
        return

    STATE["last_alert_utc"] = now

    send_text(
        f"{alert['emoji']} *[CorrelWatch] {alert['type']}*\n"
        f"{alert['signal']}\n"
        f"{alert['note']}\n"
        f"Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}"
    )
    log.info(f"CorrelWatch alert fired: {alert['type']}")
