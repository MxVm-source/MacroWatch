# bot/modules/cryptowatch_daily.py

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import os

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


def now_tz() -> datetime:
    return datetime.now(TZ)


def _fmt_usd(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.0f}"


def fetch_daily_metrics() -> dict:
    """
    Daily metrics for CryptoWatch.
    - BTC / ETH prices from Bitget (same symbols as FedWatch).
    - Other fields currently static placeholders (can be wired later).
    """
    btc_raw = None
    eth_raw = None

    btc_sym = os.getenv("FED_REACT_BTC_SYMBOL", "BTCUSDT_UMCBL")
    eth_sym = os.getenv("FED_REACT_ETH_SYMBOL", "ETHUSDT_UMCBL")

    try:
        btc_raw = get_ticker(btc_sym)
    except Exception as e:
        log.warning("CryptoWatch: failed to fetch BTC price: %s", e)

    try:
        eth_raw = get_ticker(eth_sym)
    except Exception as e:
        log.warning("CryptoWatch: failed to fetch ETH price: %s", e)

    btc_price = _fmt_usd(float(btc_raw)) if btc_raw is not None else "N/A"
    eth_price = _fmt_usd(float(eth_raw)) if eth_raw is not None else "N/A"

    # NOTE: 24h % changes, total MC, DXY, SPX, etc. are still placeholders for now.
    return {
        "sentiment": "Bearish / Cautious",
        "fg_value": 20,
        "fg_label": "Extreme Fear",
        "overnight_tone": "Weak bounce attempts sold into; risk-off tone persists.",

        "btc_price": btc_price,
        "btc_24h": -1.8,
        "eth_price": eth_price,
        "eth_24h": -2.3,
        "total_mc": "$3.05T",
        "total_mc_24h": -1.9,

        "funding_rate": "Slightly negative (favoring shorts)",
        "oi_change_24h": -2.7,
        "liq_long": "$210M",
        "liq_short": "$85M",

        "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
        "dxy_value": "104.8",
        "dxy_change_24h": 0.3,
        "spx_fut": "4,950",
        "spx_fut_pct": -0.4,
        "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",

        "reg_or_news_1": "Market watching ongoing exchange and stablecoin oversight discussions.",
        "reg_or_news_2": "Selective headlines around DeFi and offshore venues add to caution.",

        "bias": "Bearish bias unless BTC reclaims key resistance.",
        "btc_key_level": "$90,000",
    }


def generate_ai_comment(metrics: dict) -> str:
    """
    Use an LLM (OpenAI) to generate a short trader-focused analysis
    based on today's metrics.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("CryptoWatch: OPENAI_API_KEY not set, skipping AI analysis.")
        return "AI analysis disabled (no API key configured)."

    try:
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        # Build a compact, structured summary for the model
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

        prompt_system = (
            "You are a professional crypto and macro trader. "
            "You write short, high-signal market briefs for other traders. "
            "Be concise, actionable and avoid any financial advice language."
        )

        prompt_user = (
            "Using the data below, write a 3–6 sentence market take for crypto traders "
            "before the U.S. cash session. Discuss:\n"
            "- overall risk mood (risk-on/off)\n"
            "- BTC/ETH context\n"
            "- impact of macro & dollar\n"
            "- what kind of day to expect (choppy, trending, squeeze risk, etc.)\n\n"
            "Keep it tight, no emojis, no disclaimers.\n\n"
            f"DATA:\n{data_snippet}"
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt_system},
                {"role": "user", "content": prompt_user},
            ],
            temperature=0.7,
            max_tokens=400,
        )

        ai_text = resp.choices[0].message.content.strip()
        return ai_text or "No AI analysis generated."
    except Exception as e:
        log.error("CryptoWatch: AI generation failed: %s", e)
        return "AI analysis temporarily unavailable."


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
