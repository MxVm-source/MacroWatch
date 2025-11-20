# bot/modules/cryptowatch_daily.py

import logging
import os
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo
import requests
from openai import OpenAI

from bot.utils import send_text
from bot.datafeed_bitget import get_ticker

DAILY_BRIEF_TEMPLATE = """🧠 [CryptoWatch] Daily Market Brief
📅 {date} — Before U.S. Market Open

🔻 Sentiment: {sentiment}
Fear & Greed Index: {fg_value}/100 → {fg_label}
Overnight Tone: {overnight_tone}

💰 Market Snapshot
• BTC: {btc_price} ({btc_24h}% / 24h)
• ETH: {eth_price} ({eth_24h}% / 24h)
• TOTAL MC: {total_mc} ({total_mc_24h}%)

• Futures (BTC):
  - Funding: {funding_rate}
  - Open Interest: {oi_change_24h}%
  - Liquidations (12h): L {liq_long} / S {liq_short}

🌎 Macro Snapshot
• U.S. mood: {us_macro}
• Dollar Index (DXY): {dxy_value} ({dxy_change_24h}%)
• S&P Futures: {spx_fut} ({spx_fut_pct}%)
• Key event today: {macro_event}

⚖️ Regulation & News
• {reg_or_news_1}
• {reg_or_news_2}

📈 Bias for Today: {bias}
Key Level BTC: {btc_key_level}

🤖 AI Market Take
{ai_comment}

⚠️ Note: Brief sentiment scan — not financial advice.
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_daily")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Brussels"))


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def now_tz() -> datetime:
    return datetime.now(TZ)


def _fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.0f}"


def _fmt_pct(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"{val:.2f}"


# --------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------
def get_fear_greed():
    """Fear & Greed Index from alternative.me"""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        data = r.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        log.warning("CryptoWatch: Fear & Greed fetch failed: %s", e)
        return None, None


def get_crypto_changes():
    """
    BTC / ETH price + 24h change.

    Primary source: CoinGecko
    Fallback: Bitget ticker (price only, no 24h change)
    Returns (btc_price, btc_24h, eth_price, eth_24h) in USD.
    """
    # --- Primary: CoinGecko ---
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "bitcoin,ethereum",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        btc = data["bitcoin"]
        eth = data["ethereum"]
        return (
            float(btc["usd"]),
            float(btc.get("usd_24h_change", 0.0)),
            float(eth["usd"]),
            float(eth.get("usd_24h_change", 0.0)),
        )
    except Exception as e:
        log.warning("CryptoWatch: CoinGecko BTC/ETH fetch failed, trying Bitget fallback: %s", e)

    # --- Fallback: Bitget (price only, no % change) ---
    try:
        btc_sym = os.getenv("FED_REACT_BTC_SYMBOL", "BTCUSDT_UMCBL")
        eth_sym = os.getenv("FED_REACT_ETH_SYMBOL", "ETHUSDT_UMCBL")

        btc_raw = get_ticker(btc_sym)
        eth_raw = get_ticker(eth_sym)

        btc_price = float(btc_raw) if btc_raw is not None else None
        eth_price = float(eth_raw) if eth_raw is not None else None

        return btc_price, None, eth_price, None
    except Exception as e:
        log.warning("CryptoWatch: Bitget BTC/ETH fallback failed: %s", e)
        return None, None, None, None


def get_total_market_cap():
    """
    Total crypto market cap from CoinGecko /global.
    Returns (market_cap_usd, pct_change_24h).
    """
    try:
        url = "https://api.coingecko.com/api/v3/global"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()["data"]
        mc = float(data["total_market_cap"]["usd"])
        pct = float(data.get("market_cap_change_percentage_24h_usd", 0.0))
        return mc, pct
    except Exception as e:
        log.warning("CryptoWatch: total market cap fetch failed: %s", e)
        return None, None


def _fetch_stooq_csv(symbol: str) -> Optional[str]:
    """
    Fetch latest quote row from Stooq CSV.
    Example URL: https://stooq.com/q/l/?s=^dxy&i=d
    """
    try:
        url = f"https://stooq.com/q/l/?s={symbol}&i=d"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        return lines[1]  # header is line 0, data is line 1
    except Exception as e:
        log.warning("CryptoWatch: Stooq fetch failed for %s: %s", symbol, e)
        return None


def get_dxy():
    """
    Dollar index via Stooq (^dxy).
    Returns (last_price, change_pct or None).
    We only have last close here, so change_pct = None => N/A.
    """
    row = _fetch_stooq_csv("^dxy")
    if not row:
        return None, None
    # Format: SYMBOL,DATE,TIME,OPEN,HIGH,LOW,CLOSE,VOLUME
    parts = row.split(",")
    if len(parts) < 7:
        return None, None
    try:
        close = float(parts[6])
        return close, None
    except Exception:
        return None, None


def get_spx_futures():
    """
    S&P 500 futures via Stooq (es.f).
    Returns (last_price, change_pct or None).
    """
    row = _fetch_stooq_csv("es.f")
    if not row:
        return None, None
    parts = row.split(",")
    if len(parts) < 7:
        return None, None
    try:
        close = float(parts[6])
        return close, None
    except Exception:
        return None, None


# --------------------------------------------------------------------
# Build metrics (single place)
# --------------------------------------------------------------------
def fetch_daily_metrics() -> dict:
    """
    Collects all data needed for the daily brief.
    Falls back gracefully if any source fails.
    """

    # Fear & Greed
    fg_value, fg_label = get_fear_greed()
    if fg_value is None:
        fg_value_display = "N/A"
        fg_label_display = "Unknown"
    else:
        fg_value_display = fg_value
        fg_label_display = fg_label

    # BTC / ETH
    btc_usd, btc_24h, eth_usd, eth_24h = get_crypto_changes()

    # Total Market Cap
    total_mc, total_mc_pct = get_total_market_cap()

    # DXY
    dxy_val, dxy_pct = get_dxy()

    # S&P futures
    spx_val, spx_pct = get_spx_futures()

    # High-level sentiment heuristic
    if isinstance(fg_value, int) and fg_value < 30:
        sentiment = "Bearish / Cautious"
    elif isinstance(fg_value, int) and fg_value > 60:
        sentiment = "Bullish / Risk-on"
    else:
        sentiment = "Neutral / Two-sided flow"

    overnight_tone = "Weak bounce attempts sold into; risk-off tone persists."

    metrics = {
        "sentiment": sentiment,
        "fg_value": fg_value_display,
        "fg_label": fg_label_display,
        "overnight_tone": overnight_tone,
        "btc_price": _fmt_usd(btc_usd),
        "btc_24h": _fmt_pct(btc_24h),
        "eth_price": _fmt_usd(eth_usd),
        "eth_24h": _fmt_pct(eth_24h),
        "total_mc": (
            f"${total_mc/1e12:.2f}T" if isinstance(total_mc, (int, float)) else "N/A"
        ),
        "total_mc_24h": _fmt_pct(total_mc_pct),
        # Still mostly static for now – you can later wire real derivatives feeds here
        "funding_rate": "Slightly negative (favoring shorts)",
        "oi_change_24h": -2.7,
        "liq_long": "$210M",
        "liq_short": "$85M",
        "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
        "dxy_value": dxy_val if dxy_val is not None else "N/A",
        "dxy_change_24h": _fmt_pct(dxy_pct),
        "spx_fut": f"{spx_val:,.0f}" if spx_val is not None else "N/A",
        "spx_fut_pct": _fmt_pct(spx_pct),
        "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
        "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
        "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
        "bias": "Bearish bias unless BTC reclaims key resistance.",
        "btc_key_level": "$90,000",
    }

    return metrics


# --------------------------------------------------------------------
# AI analysis –