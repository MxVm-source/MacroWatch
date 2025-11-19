# cryptowatch.py
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.settings import (
    TELEGRAM_BOT_TOKEN,
    CRYPTOWATCH_CHAT_ID,
    TIMEZONE,
    CRYPTOWATCH_ENABLED,
)
from bot.template import CRYPTO_WATCH_TEMPLATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch")


def now_tz():
    return datetime.now(ZoneInfo(TIMEZONE))


def get_week_bounds(now):
    monday = now - timedelta(days=now.weekday())  # Monday = 0
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


def send_telegram(text):
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

    r = requests.post(url, json=payload)
    if r.status_code != 200:
        log.error("Telegram error: %s %s", r.status_code, r.text)
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
