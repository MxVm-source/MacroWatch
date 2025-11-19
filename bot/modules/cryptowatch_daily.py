# bot/modules/cryptowatch_daily.py

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import os

from bot.utils import send_text

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

⚠️ Note: Brief sentiment scan — not financial advice.
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_daily")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Brussels"))


def now_tz() -> datetime:
    return datetime.now(TZ)


def fetch_daily_metrics() -> dict:
    """Placeholder metrics so the pipeline works end-to-end."""
    return {
        "sentiment": "Bearish / Cautious",
        "fg_value": 20,
        "fg_label": "Extreme Fear",
        "overnight_tone": "Weak bounce attempts sold into; risk-off tone persists.",

        "btc_price": "$88,900",
        "btc_24h": -1.8,
        "eth_price": "$3,150",
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


def build_message() -> str:
    now = now_tz()
    metrics = fetch_daily_metrics()
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
