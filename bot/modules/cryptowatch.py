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
FINNHUB_KEY  = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE = "https://finnhub.io/api/v1"

FINNHUB_SYMBOLS = {
    "DXY":   "FOREX:USDX",
    "SPX":   "^GSPC",
    "US10Y": "^TNX",
    "Gold":  "OANDA:XAU_USD",
}

MODEL = os.getenv("CRYPTOWATCH_WEEKLY_MODEL", "gpt-4.1-mini")

# ─── System prompt ────────────────────────────────────────────────────────────

WEEKLY_SYSTEM_PROMPT = """You are CryptoWatch, a sharp desk strategist sending a Sunday evening weekly brief to ETH/BTC perp traders.

HARD RULES:
- 200-280 words MAX. Hard limit. Every word must earn its place.
- Write in flowing prose, not bullet points. No headers with dashes, no sub-bullets.
- Skip any macro point where data is missing — don't fill with "no data available" filler.
- Never say you are an AI. Never add disclaimers.
- If macro overlay is missing or sparse, skip the macro section entirely and keep it to crypto structure only.
- Confident and direct. No hedging language like "suggests", "may", "could potentially".

OUTPUT FORMAT (strict — prose only, minimal headers):

🧠 [CryptoWatch] {week_start} → {week_end}

[One sentence. Week's dominant theme. Max 15 words.]

📊 Structure
[2-3 sentences max. BTC weekly candle character, key level to watch, structure intact or breaking. ETH vs BTC in one clause. No separate ETH section.]

🌍 Macro
[2 sentences max. Only include if Finnhub macro overlay has real data. DXY + rates + SPX in one read. Skip entirely if no data.]

⚡ Next Week
[2-3 sentences. Bias, what changes it, one specific thing to watch. No bullet points.]

Total: under 280 words. If you exceed 280 words you have failed.
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

def _finnhub_quote(symbol: str) -> dict | None:
    if not FINNHUB_KEY:
        return None
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/quote",
            params={"symbol": symbol, "token": FINNHUB_KEY},
            timeout=6,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("c"):
            return None
        return {
            "last":       float(data["c"]),
            "prev_close": float(data.get("pc") or data["c"]),
            "change_pct": float(data.get("dp") or 0),
        }
    except Exception as e:
        log.warning(f"Finnhub quote failed for {symbol}: {e}")
        return None


def _macro_point(name: str, symbol: str) -> dict | None:
    q = _finnhub_quote(symbol)
    if not q:
        return None
    chg = q["change_pct"]
    direction = "flat" if abs(chg) < 0.05 else ("up" if chg > 0 else "down")
    return {"name": name, "last": q["last"], "change_pct": round(chg, 3),
            "direction": direction}


def fetch_macro_overlay() -> dict:
    overlay = {"source": "finnhub", "items": []}
    for name, sym in [
        ("US10Y", FINNHUB_SYMBOLS["US10Y"]),
        ("DXY",   FINNHUB_SYMBOLS["DXY"]),
        ("S&P 500", FINNHUB_SYMBOLS["SPX"]),
        ("Gold",  FINNHUB_SYMBOLS["Gold"]),
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
