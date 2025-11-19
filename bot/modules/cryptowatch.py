# bot/modules/cryptowatch.py

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

from bot.utils import send_text

CRYPTO_WATCH_TEMPLATE = """🧠 [CryptoWatch] Weekly Crypto Market Sentiment
📅 Week: {week_start} → {week_end}

🔻 General Mood: {general_mood}
Fear & Greed Index (Weekly Range): {fg_low}–{fg_high}/100 → “{fg_label}”
Weekly Bias: {weekly_bias}
Market Stress: {market_stress}

💰 Price & Market Pressure (This Week)
• Bitcoin (BTC):
  - Weekly close: {btc_close}
  - Weekly change: {btc_weekly_pct}%
  - High / Low: {btc_high} / {btc_low}
  - Key narrative: {btc_narrative}

• Total Crypto Market Cap:
  - Current: {total_mc}
  - Weekly change: {total_mc_weekly_pct}%
  - From recent peak: {total_mc_from_peak_pct}%

• Altcoins:
  - Avg drawdown from recent highs: {alts_avg_drawdown}%
  - Typical range this week: {alts_range_drawdown}%
  - Altcoin tone: {alts_tone}

— — — — — — — — — —

🧾 Contributing Factors (This Week)

1️⃣ Macro Headwinds / Tailwinds
• Main macro theme: {macro_main_theme}
• Key events:
  - {macro_event_1}
  - {macro_event_2}
• Net macro impact on crypto: {macro_impact}

2️⃣ Liquidity & Flows
• Spot volumes: {spot_volume_status}
• Derivatives:
  - Open interest (WoW): {open_interest_wow_pct}%
  - Liquidations (7D): Longs: {long_liq_total} / Shorts: {short_liq_total}
• Exchange net flows: {exchange_net_flows_7d}
• ETF / fund flows: {etf_flows_status}

3️⃣ Regulation & Policy
• U.S. headline this week: {us_reg_highlight}
• EU headline this week: {eu_reg_highlight}
• Other key jurisdiction: {other_reg_highlight}
• Overall regulatory tone: {reg_tone}

4️⃣ Market Psychology
• Retail behavior: {retail_behavior}
• Social/media sentiment: {social_sentiment}
• Dominant emotions: {dominant_emotions}

— — — — — — — — — —

📈 Counterpoint – Opportunity View
• Contrarian perspective: {contrarian_view}
• On-chain:
  - Long-term holders: {lth_behavior}
  - Short-term holders: {sth_behavior}
  - Capitulation signs: {onchain_capitulation_status}
• Structural metrics:
  - Activity trend: {activity_trend}
  - Concentration (whales vs retail): {concentration_comment}

— — — — — — — — — —

✅ Weekly Summary
• One-liner: {weekly_one_liner}
• Core takeaway:
  - {key_takeaway_1}
  - {key_takeaway_2}

• Risk outlook for next week: {next_week_outlook}

📌 Note: This is a sentiment + context report, not financial advice.
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_weekly")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Brussels"))


def now_tz():
    return datetime.now(TZ)


def get_week_bounds(now):
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    return monday.date(), sunday.date()


def fetch_weekly_metrics():
    """Static placeholder. Replace with real API calls when ready."""
    return {
        "general_mood": "Bearish / Fearful",
        "fg_low": 15,
        "fg_high": 25,
        "fg_label": "Extreme Fear",
        "weekly_bias": "Risk-off with heavy de-risking",
        "market_stress": "High volatility, aggressive selling",

        "btc_close": "$88,500",
        "btc_weekly_pct": -6.8,
        "btc_high": "$94,200",
        "btc_low": "$86,900",
        "btc_narrative": "Failed to hold above 90k, selling pressure escalating",

        "total_mc": "$3.0T",
        "total_mc_weekly_pct": -7.5,
        "total_mc_from_peak_pct": -12.0,

        "alts_avg_drawdown": 25,
        "alts_range_drawdown": "20–40",
        "alts_tone": "High beta coins crushed, memecoins bleeding",

        "macro_main_theme": "Fading U.S. rate cut hopes",
        "macro_event_1": "Hawkish central bank comments",
        "macro_event_2": "Dollar strength pressuring risk assets",
        "macro_impact": "Bearish",

        "spot_volume_status": "High on sell-offs, weak on bounces",
        "open_interest_wow_pct": -5.2,
        "long_liq_total": "$820M",
        "short_liq_total": "$240M",
        "exchange_net_flows_7d": "Net inflows (risk of sell-side pressure)",
        "etf_flows_status": "Muted to negative",

        "us_reg_highlight": "Talks of stricter stablecoin frameworks",
        "eu_reg_highlight": "MiCA rollout uncertainty",
        "other_reg_highlight": "Crackdowns on offshore venues",
        "reg_tone": "Cautious / Nervous",

        "retail_behavior": "Panic selling, sidelining",
        "social_sentiment": "Fear, frustration, capitulation",
        "dominant_emotions": "Fear, doubt, exhaustion",

        "contrarian_view": "Extreme fear often precedes accumulation",
        "lth_behavior": "Holding / some accumulation",
        "sth_behavior": "Heavy realized losses and capitulation",
        "onchain_capitulation_status": "Elevated",

        "activity_trend": "Active addresses down from peak",
        "concentration_comment": "Whales stable to slightly accumulating",

        "weekly_one_liner": "Fear-heavy week with altcoins deeply underwater.",
        "key_takeaway_1": "Macro + regulation driving risk-off behavior.",
        "key_takeaway_2": "On-chain shows weak-hand capitulation.",
        "next_week_outlook": "Fragile conditions — key BTC levels in focus.",
    }


def build_message():
    now = now_tz()
    week_start, week_end = get_week_bounds(now)
    m = fetch_weekly_metrics()

    return CRYPTO_WATCH_TEMPLATE.format(
        week_start=week_start,
        week_end=week_end,
        **m,
    )


def main():
    # Feature flag if you ever want to disable it via env
    if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() not in ("1", "true", "yes", "on"):
        log.info("CryptoWatch weekly disabled via ENABLE_CRYPTOWATCH_WEEKLY.")
        return

    msg = build_message()
    send_text(msg)
    log.info("CryptoWatch weekly report sent.")

4️⃣ Market Psychology
• Retail behavior: {retail_behavior}
• Social/media sentiment: {social_sentiment}
• Dominant emotions: {dominant_emotions}

— — — — — — — — — —

📈 Counterpoint – Opportunity View
• Contrarian perspective: {contrarian_view}
• On-chain:
  - Long-term holders: {lth_behavior}
  - Short-term holders: {sth_behavior}
  - Capitulation signs: {onchain_capitulation_status}
• Structural metrics:
  - Activity trend: {activity_trend}
  - Concentration (whales vs retail): {concentration_comment}

— — — — — — — — — —

✅ Weekly Summary
• One-liner: {weekly_one_liner}
• Core takeaway:
  - {key_takeaway_1}
  - {key_takeaway_2}

• Risk outlook for next week: {next_week_outlook}

📌 Note: This is a sentiment + context report, not financial advice.
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_weekly")


def now_tz():
    return datetime.now(ZoneInfo(TIMEZONE))


def get_week_bounds(now):
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    return monday.date(), sunday.date()


def fetch_weekly_metrics():
    """Static placeholder. Replace with real API calls when ready."""
    return {
        "general_mood": "Bearish / Fearful",
        "fg_low": 15,
        "fg_high": 25,
        "fg_label": "Extreme Fear",
        "weekly_bias": "Risk-off with heavy de-risking",
        "market_stress": "High volatility, aggressive selling",

        "btc_close": "$88,500",
        "btc_weekly_pct": -6.8,
        "btc_high": "$94,200",
        "btc_low": "$86,900",
        "btc_narrative": "Failed to hold above 90k, selling pressure escalating",

        "total_mc": "$3.0T",
        "total_mc_weekly_pct": -7.5,
        "total_mc_from_peak_pct": -12.0,

        "alts_avg_drawdown": 25,
        "alts_range_drawdown": "20–40",
        "alts_tone": "High beta coins crushed, memecoins bleeding",

        "macro_main_theme": "Fading U.S. rate cut hopes",
        "macro_event_1": "Hawkish central bank comments",
        "macro_event_2": "Dollar strength pressuring risk assets",
        "macro_impact": "Bearish",

        "spot_volume_status": "High on sell-offs, weak on bounces",
        "open_interest_wow_pct": -5.2,
        "long_liq_total": "$820M",
        "short_liq_total": "$240M",
        "exchange_net_flows_7d": "Net inflows (risk of sell-side pressure)",
        "etf_flows_status": "Muted to negative",

        "us_reg_highlight": "Talks of stricter stablecoin frameworks",
        "eu_reg_highlight": "MiCA rollout uncertainty",
        "other_reg_highlight": "Crackdowns on offshore venues",
        "reg_tone": "Cautious / Nervous",

        "retail_behavior": "Panic selling, sidelining",
        "social_sentiment": "Fear, frustration, capitulation",
        "dominant_emotions": "Fear, doubt, exhaustion",

        "contrarian_view": "Extreme fear often precedes accumulation",
        "lth_behavior": "Holding / some accumulation",
        "sth_behavior": "Heavy realized losses and capitulation",
        "onchain_capitulation_status": "Elevated",

        "activity_trend": "Active addresses down from peak",
        "concentration_comment": "Whales stable to slightly accumulating",

        "weekly_one_liner": "Fear-heavy week with altcoins deeply underwater.",
        "key_takeaway_1": "Macro + regulation driving risk-off behavior.",
        "key_takeaway_2": "On-chain shows weak-hand capitulation.",
        "next_week_outlook": "Fragile conditions — key BTC levels in focus.",
    }


def build_message():
    now = now_tz()
    week_start, week_end = get_week_bounds(now)
    m = fetch_weekly_metrics()

    return CRYPTO_WATCH_TEMPLATE.format(
        week_start=week_start,
        week_end=week_end,
        **m,
    )


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not CRYPTOWATCH_CHAT_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or CRYPTOWATCH_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CRYPTOWATCH_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        log.error("Telegram error: %s %s", resp.status_code, resp.text)
    else:
        log.info("CryptoWatch weekly report sent.")


def main():
    if not CRYPTOWATCH_ENABLED:
        log.info("CryptoWatch disabled.")
        return

    msg = build_message()
    send_telegram(msg)


if __name__ == "__main__":
    main()
