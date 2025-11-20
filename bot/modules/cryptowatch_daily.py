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
# Data sources (only stable ones)
# --------------------------------------------------------------------
def get_fear_greed():
    """Fear & Greed Index from alternative.me (single lightweight call)."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        r.raise_for_status()
        data = r.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        log.warning("CryptoWatch: Fear & Greed fetch failed: %s", e)
        return None, None


def get_btc_eth_from_bitget():
    """
    Use your existing Bitget datafeed for BTC/ETH prices.
    Only last price, no 24h change available from get_ticker,
    so we treat change as 0.0 for now.
    """
    btc_price = None
    eth_price = None

    btc_sym = os.getenv("FED_REACT_BTC_SYMBOL", "BTCUSDT_UMCBL")
    eth_sym = os.getenv("FED_REACT_ETH_SYMBOL", "ETHUSDT_UMCBL")

    try:
        btc_raw = get_ticker(btc_sym)
        if btc_raw is not None:
            btc_price = float(btc_raw)
    except Exception as e:
        log.warning("CryptoWatch: BTC Bitget fetch failed: %s", e)

    try:
        eth_raw = get_ticker(eth_sym)
        if eth_raw is not None:
            eth_price = float(eth_raw)
    except Exception as e:
        log.warning("CryptoWatch: ETH Bitget fetch failed: %s", e)

    # We don't have 24h change from this helper yet; treat as flat (0.00)
    return btc_price, 0.0, eth_price, 0.0


# --------------------------------------------------------------------
# Build metrics
# --------------------------------------------------------------------
def fetch_daily_metrics() -> dict:
    """
    Collects all data needed for the daily brief using only
    stable, low-rate-limit sources:
    - Bitget for BTC/ETH
    - Fear & Greed from alternative.me
    Other macro fields remain static for now to avoid API spam.
    """

    # Fear & Greed
    fg_value, fg_label = get_fear_greed()
    if fg_value is None:
        fg_value_display = "N/A"
        fg_label_display = "Unknown"
    else:
        fg_value_display = fg_value
        fg_label_display = fg_label

    # BTC / ETH from Bitget
    btc_usd, btc_24h, eth_usd, eth_24h = get_btc_eth_from_bitget()

    # Simple sentiment heuristic based on F&G
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

        # To avoid more APIs, keep total MC static/placeholder for now
        "total_mc": "N/A",
        "total_mc_24h": "N/A",

        # Futures / derivatives still placeholders
        "funding_rate": "Slightly negative (favoring shorts)",
        "oi_change_24h": -2.7,
        "liq_long": "$210M",
        "liq_short": "$85M",

        # Macro snapshot mostly static for now
        "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
        "dxy_value": "N/A",
        "dxy_change_24h": "N/A",
        "spx_fut": "N/A",
        "spx_fut_pct": "N/A",
        "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",

        "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
        "reg_or_news_2": "Some pressure around DeFi and offshore venues.",

        "bias": "Bearish bias unless BTC reclaims key resistance.",
        "btc_key_level": "$90,000",
    }

    return metrics


# --------------------------------------------------------------------
# AI analysis
# --------------------------------------------------------------------
def generate_ai_comment(metrics: dict) -> str:
    """
    Use OpenAI to generate a short trader-focused daily market take.
    Works even with partial data; unknown fields will show as N/A.
    """

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("CryptoWatch: OPENAI_API_KEY not set, skipping AI analysis.")
        return "AI analysis disabled (no API key configured)."

    try:
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        data_snippet = (
            f"Date: {datetime.utcnow().date().isoformat()}\n"
            f"Sentiment: {metrics['sentiment']}\n"
            f"Fear & Greed: {metrics['fg_value']} ({metrics['fg_label']})\n"
            f"BTC: {metrics['btc_price']} ({metrics['btc_24h']}% / 24h)\n"
            f"ETH: {metrics['eth_price']} ({metrics['eth_24h']}% / 24h)\n"
            f"Total Market Cap: {metrics['total_mc']} ({metrics['total_mc_24h']}%)\n"
            f"Funding: {metrics['funding_rate']}\n"
            f"Open Interest 24h: {metrics['oi_change_24h']}%\n"
            f"Liquidations 12h: Longs {metrics['liq_long']} / Shorts {metrics['liq_short']}\n"
            f"Macro: {metrics['us_macro']}\n"
            f"DXY: {metrics['dxy_value']} ({metrics['dxy_change_24h']}%)\n"
            f"S&P Futures: {metrics['spx_fut']} ({metrics['spx_fut_pct']}%)\n"
            f"Key event: {metrics['macro_event']}\n"
        )

        system_msg = (
            "You are a professional crypto and macro trader. "
            "You write short, high-signal market briefs for other traders. "
            "Be concise, actionable, and avoid explicit financial advice."
        )

        user_msg = (
            "Using the data below, write a 3–6 sentence market take for crypto traders "
            "before the U.S. cash session. Discuss:\n"
            "- overall risk mood (risk-on/off)\n"
            "- BTC/ETH context\n"
            "- how macro tone might influence flows\n"
            "- what kind of day to expect (choppy, trending, squeeze risk, etc.)\n\n"
            "Keep it tight, no emojis, no disclaimers.\n\n"
            f"DATA:\n{data_snippet}"
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=400,
        )

        ai_text = resp.choices[0].message.content.strip()
        return ai_text or "No AI analysis generated."
    except Exception as e:
        log.error("CryptoWatch: AI generation failed: %s", e)
        return "AI analysis temporarily unavailable."


# --------------------------------------------------------------------
# Message builder + entrypoint
# --------------------------------------------------------------------
def build_message() -> str:
    now = now_tz()
    metrics = fetch_daily_metrics()
    metrics["ai_comment"] = generate_ai_comment(metrics)

    return DAILY_BRIEF_TEMPLATE.format(
        date=now.date().isoformat(),
        **metrics,
    )


def main() -> None:
    if os.getenv("ENABLE_CRYPTOWATCH_DAILY", "true").lower() not in ("1", "true", "yes", "on"):
        log.info("CryptoWatch daily disabled via ENABLE_CRYPTOWATCH_DAILY.")
        return

    msg = build_message()
    send_text(msg)
    log.info("CryptoWatch daily brief sent.")


if __name__ == "__main__":
    main()