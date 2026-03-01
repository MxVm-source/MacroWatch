# bot/modules/cryptowatch.py
"""
CryptoWatch Weekly — AI-generated strategic brief.

Fires every Sunday at 18:00 (scheduled in main.py).
Uses the same Stooq + Bitget data layer as cryptowatch_daily.py,
but with a wider lens: 7-day window, market structure, cycle positioning,
and a forward-looking strategic outlook for the week ahead.

Deliberately different from the daily brief:
  Daily  = tactical, price-action focused, before US open
  Weekly = strategic, macro-structural, cycle-aware, Sunday evening
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI

from bot.utils import send_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_weekly")

client     = OpenAI()
BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

# ─── Config ──────────────────────────────────────────────────────────────────

BITGET_BASE_URL = "https://api.bitget.com"
BTC_SYMBOL      = "BTCUSDT"
ETH_SYMBOL      = "ETHUSDT"
PRODUCT_TYPE    = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
STOOQ_BASE      = "https://stooq.com/q/d/l/"

STOOQ_SYMBOL_DXY   = os.getenv("STOOQ_SYMBOL_DXY",   "^dxy")
STOOQ_SYMBOL_SPX   = os.getenv("STOOQ_SYMBOL_SPX",   "^spx")
STOOQ_SYMBOL_GOLD  = os.getenv("STOOQ_SYMBOL_GOLD",  "xauusd")
STOOQ_SYMBOL_US10Y = os.getenv("STOOQ_SYMBOL_US10Y", "^tnx")

MODEL = os.getenv("CRYPTOWATCH_WEEKLY_MODEL", "gpt-4.1-mini")

# ─── System prompt ────────────────────────────────────────────────────────────

WEEKLY_SYSTEM_PROMPT = """You are CryptoWatch, an elite crypto market strategist writing a Sunday evening weekly brief.

Audience:
- Advanced crypto traders running BTC/ETH perp positions using structure, liquidity, and macro confluence.
- They want strategic context and cycle positioning — not a replay of the week's candles.
- They hate fluff, padding, and generic disclaimers.

Style:
- Confident, direct, analytical. Think senior desk strategist, not newsletter writer.
- Short dense paragraphs + bullet points where needed.
- Emojis sparingly for section headers only.
- Never say you are an AI. Never add "not financial advice" disclaimers.
- Assume USD unless stated.

KEY DISTINCTION — this is the WEEKLY brief, NOT the daily:
- Daily = tactical (price action, levels, today's open)
- Weekly = STRATEGIC (macro regime, cycle phase, structural bias, what matters next week)
- Do NOT repeat yesterday's price action in detail. Summarise the week in one line and move on.
- Focus on: regime, structure, narrative shifts, what to watch next week.

INPUT JSON:
- snapshot.btc / snapshot.eth: Bitget futures ticker (current)
- snapshot.weekly_range.btc / .eth: 7-day high/low/open/close derived from candles
- snapshot.meta.macro_overlay: Stooq data (DXY, US10Y, SPX, Gold)

STRUCTURE (follow this order, every section required):

1) Header
   - Line 1: "🧠 [CryptoWatch] Weekly Strategic Brief"
   - Line 2: "📅 Week of {week_start} → {week_end}"

2) Week in One Line
   - Single sentence summarising the week's dominant theme (e.g. "Risk-off flush into macro support, partial recovery into close.")

3) Macro Regime
   - Current macro regime: Risk-On / Risk-Off / Transitioning.
   - DXY direction and what it implies for crypto.
   - US10Y direction and rate narrative.
   - SPX relationship with crypto this week (correlated / decoupled).
   - Gold as risk hedge signal.
   - Net macro verdict: Tailwind / Headwind / Neutral for crypto next week.

4) BTC Structural Read
   - Weekly candle character (strong close / weak close / wick rejection / inside bar etc).
   - Key weekly support and resistance levels to watch next week.
   - Market structure: uptrend / downtrend / range — is it intact or breaking?
   - Dominant narrative driving BTC this cycle phase.

5) ETH vs BTC
   - Did ETH outperform or underperform BTC this week?
   - What does the ETH/BTC ratio suggest about altcoin risk appetite?
   - One sentence on altcoin season probability.

6) Liquidity & Positioning
   - Funding rate bias (positive = longs paying = crowded / negative = shorts paying).
   - Where is the likely liquidity sitting above/below (high-level, no precise entries).
   - Are open interest levels elevated, compressed, or neutral?

7) Key Themes for Next Week
   - 3 bullet points: the most important macro/structural themes traders should track next week.
   - Be specific (e.g. "FOMC minutes Wednesday — watch for rate path language shift").

8) Strategic Bias
   - Net bias for next week: Bullish / Bearish / Neutral / Choppy.
   - One sentence on what would CHANGE that bias (what to watch as invalidation).
   - Volatility setup for next week: expansion / contraction / event-driven spike.

Keep the brief ~350–550 words. Dense, not padded. No trade plans, no entries/stops/TPs.
"""

# ─── Bitget helpers ───────────────────────────────────────────────────────────

def _public_get(path: str, params: dict | None = None) -> dict | None:
    try:
        r = requests.get(f"{BITGET_BASE_URL}{path}", params=params or {}, timeout=6)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if data.get("code") == "00000" else None
    except Exception as e:
        log.warning(f"Bitget request failed: {e}")
        return None


def _parse_ticker(data: dict) -> dict | None:
    items = (data or {}).get("data") or []
    if not isinstance(items, list) or not items:
        return None
    t = items[0]
    def _f(k):
        v = t.get(k)
        try: return float(v) if v not in (None, "") else None
        except: return None
    return {
        "last":        _f("lastPr"),
        "high24h":     _f("high24h"),
        "low24h":      _f("low24h"),
        "change24h":   _f("change24h"),
        "fundingRate": _f("fundingRate"),
        "markPrice":   _f("markPrice"),
    }


def _fetch_weekly_range(symbol: str) -> dict | None:
    """Fetch 4H candles and derive 7-day OHLC range."""
    try:
        raw = _public_get(
            "/api/v2/mix/market/candles",
            {"symbol": symbol, "granularity": "4H", "limit": "42",  # 42 × 4H = 7 days
             "productType": PRODUCT_TYPE}
        )
        data = (raw or {}).get("data") or []
        if not data:
            return None
        closes = []
        highs  = []
        lows   = []
        for row in data:
            if isinstance(row, (list, tuple)) and len(row) >= 5:
                highs.append(float(row[2]))
                lows.append(float(row[3]))
                closes.append(float(row[4]))
        if not closes:
            return None
        return {
            "open":  closes[0],
            "close": closes[-1],
            "high":  max(highs),
            "low":   min(lows),
            "change_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 2) if closes[0] else None,
        }
    except Exception as e:
        log.warning(f"Weekly range fetch failed for {symbol}: {e}")
        return None


# ─── Stooq macro overlay ─────────────────────────────────────────────────────

def _stooq_last_two(symbol: str) -> list[dict]:
    sym = symbol.strip().lower()
    try:
        r = requests.get(f"{STOOQ_BASE}?s={quote(sym)}&i=d", timeout=6)
        if r.status_code != 200:
            return []
        lines = [ln for ln in r.text.strip().splitlines()[1:] if "," in ln][-2:]
        out = []
        for ln in lines:
            parts = ln.split(",")
            if len(parts) >= 5:
                try:
                    out.append({"date": parts[0].strip(), "close": float(parts[4].strip())})
                except Exception:
                    pass
        return out
    except Exception:
        return []


def _macro_point(name: str, symbol: str) -> dict | None:
    rows = _stooq_last_two(symbol)
    if not rows:
        return None
    last = rows[-1]["close"]
    prev = rows[-2]["close"] if len(rows) >= 2 else None
    change_pct = round((last - prev) / prev * 100, 3) if prev else None
    direction  = "flat"
    if change_pct:
        direction = "up" if change_pct > 0.05 else ("down" if change_pct < -0.05 else "flat")
    return {"name": name, "last": last, "change_pct": change_pct,
            "direction": direction, "as_of": rows[-1]["date"]}


def fetch_macro_overlay() -> dict:
    overlay = {"source": "stooq", "items": []}
    for name, sym in [
        ("US10Y (TNX proxy)", STOOQ_SYMBOL_US10Y),
        ("DXY",               STOOQ_SYMBOL_DXY),
        ("S&P 500",           STOOQ_SYMBOL_SPX),
        ("Gold",              STOOQ_SYMBOL_GOLD),
    ]:
        pt = _macro_point(name, sym)
        if pt:
            overlay["items"].append(pt)
    return overlay


# ─── Snapshot builder ─────────────────────────────────────────────────────────

def _build_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday() + 1)).date()  # last Monday
    week_end   = (now - timedelta(days=now.weekday()    )).date()  # this Sunday (today)

    snapshot = {
        "as_of_utc":   now.strftime("%Y-%m-%d %H:%M:%S"),
        "week_start":  str(week_start),
        "week_end":    str(week_end),
        "btc":         {},
        "eth":         {},
        "weekly_range": {},
        "meta":        {},
    }

    # Current ticker
    btc_raw = _public_get("/api/v2/mix/market/ticker",
                          {"productType": PRODUCT_TYPE, "symbol": BTC_SYMBOL})
    btc = _parse_ticker(btc_raw)
    if btc:
        btc["symbol"] = BTC_SYMBOL
        snapshot["btc"] = btc

    eth_raw = _public_get("/api/v2/mix/market/ticker",
                          {"productType": PRODUCT_TYPE, "symbol": ETH_SYMBOL})
    eth = _parse_ticker(eth_raw)
    if eth:
        eth["symbol"] = ETH_SYMBOL
        snapshot["eth"] = eth

    # 7-day OHLC range from 4H candles
    btc_range = _fetch_weekly_range(BTC_SYMBOL)
    eth_range = _fetch_weekly_range(ETH_SYMBOL)
    if btc_range:
        snapshot["weekly_range"]["btc"] = btc_range
    if eth_range:
        snapshot["weekly_range"]["eth"] = eth_range

    # Macro overlay
    try:
        overlay = fetch_macro_overlay()
        if overlay.get("items"):
            snapshot["meta"]["macro_overlay"] = overlay
    except Exception as e:
        log.warning(f"Macro overlay failed: {e}")

    return snapshot


# ─── OpenAI generation ───────────────────────────────────────────────────────

def _generate_brief(snapshot: dict) -> str:
    now        = datetime.now(BRUSSELS_TZ)
    week_start = snapshot.get("week_start", "")
    week_end   = snapshot.get("week_end",   "")

    payload = json.dumps(
        {"week_start": week_start, "week_end": week_end, "snapshot": snapshot},
        ensure_ascii=False
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.65,   # slightly lower than daily for more consistent strategic tone
            max_tokens=1100,
            messages=[
                {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
                {"role": "user",   "content": "Generate the WEEKLY strategic brief:\n\n" + payload},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"OpenAI call failed: {e}")
        return (
            "🧠 [CryptoWatch] Weekly Strategic Brief\n"
            "⚠️ Could not generate weekly analysis (model error).\n"
            "Review macro context manually before next week's sessions."
        )


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() not in ("1", "true", "yes", "on"):
        log.info("CryptoWatch weekly disabled via env.")
        return

    log.info("Building weekly snapshot...")
    try:
        snapshot = _build_snapshot()
    except Exception as e:
        log.exception(f"Snapshot build failed: {e}")
        send_text("🧠 [CryptoWatch] Weekly Brief\n⚠️ Could not build market snapshot.")
        return

    log.info("Generating weekly brief via OpenAI...")
    brief = _generate_brief(snapshot)
    send_text(brief)
    log.info("CryptoWatch weekly brief sent.")
