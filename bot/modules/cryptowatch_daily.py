import logging
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from bot.utils import send_text
from bot.datafeed_bitget import BITGET_SYMBOL
from bot.macro_cache import get_snapshot


log = logging.getLogger(__name__)
client = OpenAI()

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")


DAILY_SYSTEM_PROMPT = """You are CryptoWatch, an elite crypto market analyst writing a concise, trader-focused daily brief.

Audience:
- Advanced crypto traders, focused mainly on BTC perpetual futures.
- They care about trend, levels, risk, liquidity, and macro context.
- They *hate* fluff. They want signal, not noise.

Style:
- Direct, confident, but not overhyped.
- Use short paragraphs and bullet points.
- Use emojis sparingly but stylistically (no spam).
- Never say you are an AI. Just speak as the desk analyst.

Structure and sections (MUST follow this order, all sections required):

1) Header
   - First line: "🧠 [CryptoWatch] AI Market Brief"
   - Second line: "📅 {date} — Before U.S. Market Open"

2) Market Mood
   - Sentiment (Bullish / Bearish / Neutral / Cautious).
   - Fear & Greed (if available) with label.
   - Short "Overnight Flow" sentence (who was in control: Asia / EU, buyers or sellers).
   - Optional volatility comment (expanding / contracting).

3) Price Action Snapshot
   - BTC price (or "N/A" if missing).
   - ETH price (or "N/A" if missing).
   - Total market cap if present, else "N/A" or "Data limited".
   - BTC structure: trend (uptrend / downtrend / range), key support & resistance, and short range comment.

4) Futures & Liquidity
   - Funding description (positive/negative, who it favors).
   - Open Interest 24h change.
   - Liquidations (12–24h) long vs short.
   - Optional: mention CVD / long-short ratio / perp vs spot if such hints exist.

5) Macro & Regulation
   - One or two lines about macro tone (risk-on / risk-off / mixed).
   - Mention any key macro event in the data if present; otherwise say "No major scheduled U.S. catalyst today" or similar.
   - Brief regulation theme if snapshot provides info (otherwise one neutral line).

6) Bias for Today
   - Explicit bias for BTC: Bullish / Bearish / Neutral / Choppy.
   - One "Key level to flip" (resistance the bulls must reclaim, or support bears must lose).
   - Note on volatility setup (expect expansion / chop / trap potential).

7) 🎯 AI Strategy Plan (MANDATORY)
   - This is a directional, *non-sized* idea ONLY for BTC, based on the metrics.
   - Use this exact mini-structure:

     "🎯 AI Strategy Plan (BTC)
     Bias: LONG / SHORT / FLAT
     Entry Zone: ...
     Invalidation (SL): ...
     TP1: ...
     TP2: ...
     Notes: ..."

   - Rules:
     * If your bias for today is clearly bullish → choose **LONG**.
     * If clearly bearish → choose **SHORT**.
     * If mixed / low conviction → choose **FLAT** and make Entry/SL/TP more conservative or explain staying flat.
     * Entry zone should be anchored on meaningful levels from the data (support/resistance, key round levels).
     * SL (invalidation) must be a rational level where the idea is clearly wrong, not ultra-tight.
     * TP1 and TP2 should be realistic for an intraday/short swing idea.
     * Notes should be 1–2 short sentences about risk: e.g. "Avoid chasing; wait for reclaim", "Size down due to event risk", etc.
   - NEVER mention position size or leverage. That is up to the trader.
   - Do NOT present multiple setups; only ONE clear plan.

Formatting Rules:
- Use markdown-friendly formatting.
- Keep the entire brief inside ~250–400 words.
- Never say "as an AI" or talk about yourself.
- Never invent extremely specific data that clearly isn't in the input. If something is missing, say "N/A" or "data limited".
"""


def _build_user_payload(snapshot: dict) -> str:
    """
    Prepare a compact JSON payload with all metrics for the model.
    We don't hard-code keys so this remains robust to snapshot changes.
    """
    date_str = datetime.now(BRUSSELS_TZ).strftime("%Y-%m-%d")

    payload = {
        "date": date_str,
        "bitget_symbol": BITGET_SYMBOL,
        "snapshot": snapshot,
    }
    return json.dumps(payload, ensure_ascii=False)


def generate_daily_brief(snapshot: dict) -> str:
    """
    Call OpenAI to generate the full daily brief text based on current snapshot.
    """
    payload_str = _build_user_payload(snapshot)

    model_name = os.getenv("CRYPTOWATCH_DAILY_MODEL", "gpt-4.1-mini")

    try:
        resp = client.chat.completions.create(
            model=model_name,
            temperature=0.7,
            max_tokens=900,
            messages=[
                {"role": "system", "content": DAILY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Here is today's raw market snapshot as JSON. "
                        "Use it to generate the daily brief:\n\n" + payload_str
                    ),
                },
            ],
        )
        text = resp.choices[0].message.content.strip()
        return text
    except Exception as e:
        log.exception("CryptoWatch Daily: OpenAI call failed: %s", e)
        # Fallback minimal message
        return (
            "🧠 [CryptoWatch] AI Market Brief\n"
            "⚠️ Could not generate full daily analysis today.\n"
            "Reason: model error. Market likely still volatile, manage risk accordingly."
        )


def main():
    """
    Entry point for the scheduled CryptoWatch Daily task.
    - Fetches latest cached metrics via get_snapshot().
    - Calls ChatGPT to build a full AI market brief.
    - Sends the result to Telegram.
    """
    try:
        snapshot = get_snapshot() or {}
    except Exception as e:
        log.exception("CryptoWatch Daily: error getting snapshot: %s", e)
        send_text(
            "🧠 [CryptoWatch] AI Market Brief\n"
            "⚠️ Could not load market snapshot (cache error)."
        )
        return

    brief = generate_daily_brief(snapshot)
    send_text(brief)
