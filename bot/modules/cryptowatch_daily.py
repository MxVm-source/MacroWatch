# bot/modules/cryptowatch_daily.py

import logging
import os
import json
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo
import requests
from openai import OpenAI

from bot.utils import send_text
from bot.datafeed_bitget import get_ticker
from bot.macro_cache import get_snapshot

DAILY_BRIEF_TEMPLATE = """🧠 [CryptoWatch] Daily Market Brief
📅 {date} — Before U.S. Market Open

🔻 Sentiment: {sentiment}
Fear & Greed Index: {fg_value}/100 → {fg_label}
Overnight Tone: {overnight_tone}

💰 Market Snapshot
• BTC: {btc_price}
• ETH: {eth_price}
• TOTAL MC: {total_mc_block}

• Futures (BTC):
  - Funding: {funding_rate}
  - Open Interest: {oi_change_24h}%
  - Liquidations (12h): L {liq_long} / S {liq_short}

🌎 Macro Snapshot
• U.S. mood: {us_macro}
{macro_block}
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


def _extract_json_object(content: str) -> str:
    """
    Extract a JSON object from the model output.
    Handles cases where the model wraps JSON in ```json ... ``` fences.
    """
    content = content.strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.splitlines()
        inner = [ln for ln in lines if not ln.strip().startswith("```")]
        content = "\n".join(inner).strip()

    # Generic: take the first {...} block
    first = content.find("{")
    last = content.rfind("}")
    if first != -1 and last != -1 and last > first:
        return content[first:last + 1].strip()

    return content


# --------------------------------------------------------------------
# Data sources (stable only)
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
    Use existing Bitget datafeed for BTC/ETH prices.
    get_ticker returns last price; no 24h % from this helper yet.
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

    # 24h % change not available here yet; keep numeric for AI only if needed.
    return btc_price, None, eth_price, None


# --------------------------------------------------------------------
# Build metrics
# --------------------------------------------------------------------
def fetch_daily_metrics() -> dict:
    """
    Collect all data needed for the daily brief using:
    - Bitget for BTC/ETH
    - Fear & Greed from alternative.me
    - Macro snapshot from macro_cache (B+1 mode = None → N/A)
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

    # Macro snapshot (currently no external APIs, B+1 mode)
    macro = get_snapshot(force=False)
    total_mc = macro.get("total_mc")
    total_mc_24h = macro.get("total_mc_24h")
    dxy_val = macro.get("dxy")
    dxy_pct = macro.get("dxy_24h")
    spx_val = macro.get("spx")
    spx_pct = macro.get("spx_24h")

    # Simple sentiment heuristic based on Fear & Greed
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
        "btc_24h": btc_24h,  # not shown in template for now
        "eth_price": _fmt_usd(eth_usd),
        "eth_24h": eth_24h,  # not shown in template for now

        # Keep raw-ish total MC info; formatting happens in build_message
        "total_mc": total_mc,
        "total_mc_24h": total_mc_24h,

        "funding_rate": "Slightly negative (favoring shorts)",
        "oi_change_24h": -2.7,
        "liq_long": "$210M",
        "liq_short": "$85M",

        # These will be overwritten by AI
        "us_macro": "Macro tone generated by AI.",
        "macro_event": "Key event generated by AI.",
        "reg_or_news_1": "Regulation / news generated by AI.",
        "reg_or_news_2": "Regulation / news generated by AI.",

        "dxy_value": dxy_val,
        "dxy_change_24h": dxy_pct,
        "spx_fut": spx_val,
        "spx_fut_pct": spx_pct,

        "bias": "Bearish bias unless BTC reclaims key resistance.",
        "btc_key_level": "$90,000",
    }

    return metrics


# --------------------------------------------------------------------
# AI analysis (macro + regulation + market take)
# --------------------------------------------------------------------
def generate_ai_fields(metrics: dict) -> dict:
    """
    Use OpenAI to generate:
    - us_macro
    - macro_event
    - reg_or_news_1
    - reg_or_news_2
    - ai_comment (market take)
    """

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("CryptoWatch: OPENAI_API_KEY not set, skipping AI analysis.")
        return {
            "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
            "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
            "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
            "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
            "ai_comment": "AI analysis disabled (no API key configured).",
        }

    try:
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        fg_val = str(metrics.get("fg_value"))
        fg_label = str(metrics.get("fg_label"))
        btc = metrics.get("btc_price", "N/A")
        eth = metrics.get("eth_price", "N/A")

        data_snippet = (
            f"Date: {datetime.utcnow().date().isoformat()}\n"
            f"Sentiment: {metrics['sentiment']}\n"
            f"Fear & Greed: {fg_val} ({fg_label})\n"
            f"BTC: {btc}\n"
            f"ETH: {eth}\n"
            f"Funding: {metrics['funding_rate']}\n"
            f"Open Interest 24h: {metrics['oi_change_24h']}%\n"
            f"Liquidations 12h: Longs {metrics['liq_long']} / Shorts {metrics['liq_short']}\n"
        )

        system_msg = (
            "You are a professional crypto macro analyst. "
            "Return ONLY valid JSON. No commentary. No code fences."
        )

        user_msg = (
            "Using the data below, return a JSON object with:\n"
            "  us_macro: short sentence on current U.S. macro mood\n"
            "  macro_event: key event today\n"
            "  reg_or_news_1: short line on regulation/news tone\n"
            "  reg_or_news_2: second line on regulation/structural themes\n"
            "  ai_comment: 3–6 sentence market take for crypto traders before U.S. cash session\n\n"
            "DATA:\n"
            f"{data_snippet}"
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=600,
        )

        content = resp.choices[0].message.content.strip()
        json_str = _extract_json_object(content)

        try:
            obj = json.loads(json_str)
        except Exception as e:
            log.error("CryptoWatch: parse error: %s | raw=%r | extracted=%r", e, content, json_str)
            return {
                "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
                "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
                "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
                "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
                "ai_comment": content or "AI analysis temporarily unavailable.",
            }

        return {
            "us_macro": obj.get("us_macro", "Cautious ahead of U.S. data and Fed speakers."),
            "macro_event": obj.get("macro_event", "Key U.S. data + Fed commentary on rates/inflation."),
            "reg_or_news_1": obj.get("reg_or_news_1", "Watching exchange + stablecoin oversight developments."),
            "reg_or_news_2": obj.get("reg_or_news_2", "Some pressure around DeFi and offshore venues."),
            "ai_comment": obj.get("ai_comment", "AI analysis temporarily unavailable."),
        }

    except Exception as e:
        log.error("CryptoWatch: AI generation failed: %s", e)
        return {
            "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
            "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
            "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
            "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
            "ai_comment": "AI analysis temporarily unavailable.",
        }


# --------------------------------------------------------------------
# Message builder + entrypoint
# --------------------------------------------------------------------
def build_message() -> str:
    now = now_tz()
    metrics = fetch_daily_metrics()

    # TOTAL MC display block
    total_mc = metrics.get("total_mc")
    total_mc_24h = metrics.get("total_mc_24h")
    if isinstance(total_mc, (int, float)):
        tm_str = f"${total_mc/1e12:.2f}T"
        tm_pct = _fmt_pct(total_mc_24h) if isinstance(total_mc_24h, (int, float)) else "N/A"
        metrics["total_mc_block"] = f"{tm_str} ({tm_pct}%)"
    else:
        metrics["total_mc_block"] = "Data limited today"

    # Macro block (DXY / SPX), hide N/A rows
    macro_lines = []
    dxy_val = metrics.get("dxy_value")
    dxy_pct = metrics.get("dxy_change_24h")
    if isinstance(dxy_val, (int, float)):
        macro_lines.append(f"• Dollar Index (DXY): {dxy_val} ({_fmt_pct(dxy_pct)}%)")

    spx_val = metrics.get("spx_fut")
    spx_pct = metrics.get("spx_fut_pct")
    if isinstance(spx_val, (int, float)):
        macro_lines.append(f"• S&P Futures: {spx_val:,.0f} ({_fmt_pct(spx_pct)}%)")

    if macro_lines:
        metrics["macro_block"] = "\n".join(macro_lines)
    else:
        metrics["macro_block"] = "• Macro data limited today — see AI take for context."

    # AI-generated macro + regulation + take
    ai_fields = generate_ai_fields(metrics)
    metrics.update(ai_fields)

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
