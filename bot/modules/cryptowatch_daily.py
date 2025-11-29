import logging
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI

from bot.utils import send_text

log = logging.getLogger(__name__)
client = OpenAI()

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

# ======================
# Bitget config (public endpoints only)
# ======================

BITGET_BASE_URL = "https://api.bitget.com"

# Spot symbols for easier pricing (close enough to perps for daily bias)
BTC_SPOT_SYMBOL = os.getenv("CWD_BTC_SYMBOL", "BTCUSDT")
ETH_SPOT_SYMBOL = os.getenv("CWD_ETH_SYMBOL", "ETHUSDT")


# ======================
# OpenAI prompt
# ======================

DAILY_SYSTEM_PROMPT = """You are CryptoWatch, an elite crypto market analyst writing a concise, trader-focused daily brief.

Audience:
- Advanced crypto traders, mainly trading BTC perp using a short-term 'AI strategy' (day trading, not long-term investing).
- They care about structure, levels, volatility, flow and macro context.
- They hate fluff. They want signal, not noise.

Style:
- Direct, confident, and clear.
- Use short paragraphs and bullet points.
- Use emojis sparingly but stylistically (no spam).
- Never say you are an AI. Just speak as the desk analyst.
- Assume all prices are in USD.

STRUCTURE (you MUST follow this order and include every section):

1) Header
   - Line 1: "🧠 [CryptoWatch] AI Market Brief"
   - Line 2: "📅 {date} — Before U.S. Market Open"

2) Market Mood
   - Sentiment: Bullish / Bearish / Neutral / Cautious.
   - 24h BTC performance in simple terms (e.g. "Down ~2% on the day").
   - Short "Overnight Flow" sentence (who was in control: buyers or sellers; Asia / EU if info allows).
   - Comment on volatility: expanding / contracting / elevated / muted.

3) Price Action Snapshot
   - BTC price, 24h high/low, rough range description.
   - ETH price (mention if it underperforms/outperforms BTC if data allows).
   - Total market cap: if not provided, say "Total MC: Data limited".
   - BTC structure: mention trend (uptrend / downtrend / range), nearest key support and resistance.

4) Futures & Liquidity (even if data is partial, describe what you can infer)
   - Funding bias: positive (favoring longs) vs negative (favoring shorts), or "not available".
   - Mention if price is chasing or leading (e.g. "choppy perp action with no strong conviction").
   - If you don't have OI/liquidations, just say "OI / liquidation data limited today; focus on price structure and levels."

5) Macro & Regulation
   - One short sentence on macro tone: risk-on / risk-off / mixed / data-light.
   - Mention a generic key macro catalyst if not provided: e.g. "Traders will watch U.S. data and Fed commentary for direction."
   - One line on regulation: either "No major new regulatory headlines" or a generic caution about ongoing scrutiny.

6) Bias for Today
   - Explicit BTC bias: Bullish / Bearish / Neutral / Choppy.
   - One key level BTC must flip or hold (e.g. "Bulls need to reclaim $90K", or "Bears defend $89K").
   - Volatility setup: expansion likely / range likely / trap risk.

7) 🎯 AI Strategy Plan (BTC)
   - This is the intraday AI strategy idea for BTC only.
   - Use EXACTLY this structure:

     "🎯 AI Strategy Plan (BTC)
     Bias: LONG / SHORT / FLAT
     Entry Zone: ...
     Invalidation (SL): ...
     TP1: ...
     TP2: ...
     Notes: ..."

   - Rules:
     * If your earlier bias for today is clearly bullish → choose LONG.
     * If clearly bearish → choose SHORT.
     * If mixed/low conviction → choose FLAT, and explain waiting or only scalping.
     * Entry Zone should be a small range around meaningful levels in the data (e.g. prior support/resistance, round numbers).
     * Invalidation (SL) must be a clean level where the idea is wrong, not super tight.
     * TP1 / TP2 should be realistic intraday/short-swing targets in USD.
     * Notes: 1–2 short sentences about risk/rules for this plan (e.g. "Do not chase breakouts; wait for retest", "Smaller size due to event risk", etc.).
   - Never mention leverage or position size.
   - Only one strategy plan; do NOT give alternatives.

General rules:
- Keep the entire brief roughly 250–400 words.
- Never invent obviously fake precision. It's okay to say "data limited" for missing fields.
- If some metrics (funding, OI, liqs, etc.) are missing in the input, you can say they are limited and focus more on price structure & key levels instead.
"""


# ======================
# Bitget helpers
# ======================

def _public_get(path: str, params: dict | None = None) -> dict | None:
    """Simple helper to call Bitget public endpoints safely."""
    try:
        url = f"{BITGET_BASE_URL}{path}"
        resp = requests.get(url, params=params or {}, timeout=5)
        if resp.status_code != 200:
            log.warning("CryptoWatch Daily: Bitget HTTP %s: %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        if data.get("code") != "00000":
            log.warning("CryptoWatch Daily: Bitget API error %s: %s", data.get("code"), data.get("msg"))
            return None
        return data
    except Exception as e:
        log.warning("CryptoWatch Daily: Bitget request failed: %s", e)
        return None


def _parse_spot_ticker(data: dict) -> dict | None:
    """
    Parse Bitget spot ticker payload into a friendly dict:
    { 'last': float, 'high24h': float, 'low24h': float, 'change24h': float }
    """
    if not data:
        return None

    tick = data.get("data")
    if isinstance(tick, list):
        tick = tick[0] if tick else None

    if not isinstance(tick, dict):
        return None

    def _f(key):
        v = tick.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    last = _f("close") or _f("last")
    high = _f("high24h")
    low = _f("low24h")
    change = _f("change24h") or _f("priceChangePercent24h")

    return {
        "last": last,
        "high24h": high,
        "low24h": low,
        "change24h": change,
    }


def fetch_basic_market_snapshot() -> dict:
    """Fetch BTC & ETH spot tickers from Bitget and build a simple snapshot dict."""
    snapshot: dict = {
        "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "btc": {},
        "eth": {},
        "meta": {},
    }

    # BTC
    btc_raw = _public_get("/api/v2/spot/market/ticker", {"symbol": BTC_SPOT_SYMBOL})
    btc = _parse_spot_ticker(btc_raw)
    if btc:
        snapshot["btc"] = btc
        snapshot["btc"]["symbol"] = BTC_SPOT_SYMBOL

    # ETH
    eth_raw = _public_get("/api/v2/spot/market/ticker", {"symbol": ETH_SPOT_SYMBOL})
    eth = _parse_spot_ticker(eth_raw)
    if eth:
        snapshot["eth"] = eth
        snapshot["eth"]["symbol"] = ETH_SPOT_SYMBOL

    # You can extend this later with funding/OI/liqs if desired.
    # For now we keep it simple and let the model focus on price + structure.
    snapshot["meta"]["total_market_cap"] = None  # not fetched here
    snapshot["meta"]["notes"] = "Basic BTC/ETH spot data from Bitget; no OI/liquidations in this snapshot."

    return snapshot


# ======================
# OpenAI call
# ======================

def _build_user_payload(snapshot: dict) -> str:
    """Prepare a compact JSON payload with all metrics for the model."""
    date_str = datetime.now(BRUSSELS_TZ).strftime("%Y-%m-%d")

    payload = {
        "date": date_str,
        "snapshot": snapshot,
    }
    return json.dumps(payload, ensure_ascii=False)


def generate_daily_brief(snapshot: dict) -> str:
    """Call OpenAI to generate the full daily brief text based on current snapshot."""
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
                        "Here is today's raw BTC/ETH market snapshot from Bitget (JSON). "
                        "Use it to generate the daily brief and the AI strategy plan for BTC:\n\n"
                        + payload_str
                    ),
                },
            ],
        )
        text = resp.choices[0].message.content.strip()
        return text
    except Exception as e:
        log.exception("CryptoWatch Daily: OpenAI call failed: %s", e)
        return (
            "🧠 [CryptoWatch] AI Market Brief\n"
            "⚠️ Could not generate full daily analysis today (model error).\n"
            "Assume conditions are uncertain and manage risk defensively."
        )


# ======================
# Entry point
# ======================

def main():
    """
    Scheduled CryptoWatch Daily task:
    - Fetch BTC/ETH data directly from Bitget (self-contained).
    - Call OpenAI to generate market brief + AI strategy for BTC.
    - Send to Telegram via send_text.
    """
    try:
        snapshot = fetch_basic_market_snapshot()
    except Exception as e:
        log.exception("CryptoWatch Daily: error building snapshot: %s", e)
        send_text(
            "🧠 [CryptoWatch] AI Market Brief\n"
            "⚠️ Could not build market snapshot from Bitget."
        )
        return

    brief = generate_daily_brief(snapshot)
    send_text(brief)
