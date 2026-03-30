import logging
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI

from bot.utils import send_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("cryptowatch_daily")

client = OpenAI()
BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

# ======================
# Bitget config (public futures endpoints)
# ======================

BITGET_BASE_URL = "https://api.bitget.com"

BTC_SYMBOL = "BTCUSDT"
ETH_SYMBOL = "ETHUSDT"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")  # allow env override

# ======================
# Optional macro / FedWatch integration
# ======================

FEDWATCH_DAILY_URL = os.getenv("FEDWATCH_DAILY_URL", "").strip()

# ======================
# Finnhub macro overlay
# ======================
# Free tier: 60 calls/min, no rate issues for 4 symbols/day
# Sign up at finnhub.io — instant key, no email confirmation needed
# Set FINNHUB_API_KEY in Render env vars

FINNHUB_KEY  = os.getenv("FINNHUB_API_KEY", "").strip()
FINNHUB_BASE = "https://finnhub.io/api/v1"

# Finnhub symbols
FINNHUB_SYMBOLS = {
    "DXY":   "FOREX:USDX",   # Dollar Index via forex
    "SPX":   "^GSPC",        # S&P 500
    "US10Y": "^TNX",         # 10Y Treasury yield
    "Gold":  "OANDA:XAU_USD",# Gold spot
}

# ======================
# OpenAI prompt
# ======================

DAILY_SYSTEM_PROMPT = """You are CryptoWatch — a degen crypto trader sending a morning pulse to the group before the US open.

HARD RULES:
- 80-120 words MAX. Punchy. No fluff.
- Degen tone — confident, direct, a bit edgy. Like a trader who's seen it all.
- Use emojis naturally — not every sentence, just where they add punch.
- No headers, no bullet points, no labels. Flowing prose only.
- Only use prices from the live snapshot. Never invent numbers.
- Skip macro if no clear signal — don't fill with noise.
- Fear & Greed only if extreme (<20 or >80).
- No entries, stops, TPs. No AI disclaimer.
- If market is boring, say it's boring. If it's spicy, make it feel spicy.

OUTPUT FORMAT:
One header line, then 3-5 sentences. Raw and real.

Example style:
📊 CryptoWatch — Mar 30

BTC glued to $67K, ETH waking up at +3.1% 🔥 Fear & Greed sitting at 8 — full panic mode but price refuses to dump. Funding negative across the board, shorts are piling in. That's a squeeze waiting to happen. 👀 Watching $65K — lose that and it gets ugly fast.
"""

# ======================
# Bitget helpers (V2 futures)
# ======================

def _public_get(path: str, params: dict | None = None) -> dict | None:
    try:
        url = f"{BITGET_BASE_URL}{path}"
        resp = requests.get(url, params=params or {}, timeout=5)
        if resp.status_code != 200:
            log.warning("Bitget HTTP %s: %s", resp.status_code, resp.text)
            return None
        data = resp.json()
        if data.get("code") != "00000":
            log.warning("Bitget API error %s: %s", data.get("code"), data.get("msg"))
            return None
        return data
    except Exception as e:
        log.warning("Bitget request failed: %s", e)
        return None


def _parse_mix_ticker(data: dict) -> dict | None:
    if not data:
        return None
    items = data.get("data") or []
    if not isinstance(items, list) or not items:
        return None
    tick = items[0]

    def _f(key):
        v = tick.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    return {
        "last": _f("lastPr"),
        "high24h": _f("high24h"),
        "low24h": _f("low24h"),
        "change24h": _f("change24h"),
        "fundingRate": _f("fundingRate"),
        "indexPrice": _f("indexPrice"),
        "markPrice": _f("markPrice"),
    }

# ======================
# Stooq macro overlay helpers
# ======================

def _finnhub_quote(symbol: str) -> dict | None:
    """Fetch current quote from Finnhub. Returns {c, pc, dp} or None."""
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
        # c = current, pc = previous close, dp = change percent
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


def _macro_point_finnhub(name: str, symbol: str) -> dict | None:
    q = _finnhub_quote(symbol)
    if not q:
        return None
    chg = q["change_pct"]
    direction = "flat" if abs(chg) < 0.05 else ("up" if chg > 0 else "down")
    return {
        "name":       name,
        "symbol":     symbol,
        "last":       q["last"],
        "prev_close": q["prev_close"],
        "change_pct": chg,
        "direction":  direction,
    }


def fetch_macro_overlay() -> dict:
    """Returns macro overlay dict using Finnhub."""
    overlay = {"source": "finnhub", "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    items   = []

    for name, sym in [
        ("US10Y", FINNHUB_SYMBOLS["US10Y"]),
        ("DXY",   FINNHUB_SYMBOLS["DXY"]),
        ("S&P 500", FINNHUB_SYMBOLS["SPX"]),
        ("Gold",  FINNHUB_SYMBOLS["Gold"]),
    ]:
        pt = _macro_point_finnhub(name, sym)
        if pt:
            items.append(pt)

    overlay["items"] = items
    return overlay

# ======================
# Snapshot builder
# ======================

def fetch_basic_market_snapshot() -> dict:
    snapshot: dict = {
        "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "btc": {},
        "eth": {},
        "meta": {},
    }

    btc_raw = _public_get("/api/v2/mix/market/ticker", {"productType": PRODUCT_TYPE, "symbol": BTC_SYMBOL})
    btc = _parse_mix_ticker(btc_raw)
    if btc:
        btc["symbol"] = BTC_SYMBOL
        snapshot["btc"] = btc

    eth_raw = _public_get("/api/v2/mix/market/ticker", {"productType": PRODUCT_TYPE, "symbol": ETH_SYMBOL})
    eth = _parse_mix_ticker(eth_raw)
    if eth:
        eth["symbol"] = ETH_SYMBOL
        snapshot["eth"] = eth

    snapshot["meta"]["notes"] = "BTC/ETH USDT perpetual futures data from Bitget V2 (ticker)."
    snapshot["meta"]["macro_context"] = None
    snapshot["meta"]["macro_overlay"] = None
    return snapshot

# ======================
# Macro / FedWatch helpers
# ======================

def fetch_macro_context() -> str | None:
    if not FEDWATCH_DAILY_URL:
        return None
    try:
        resp = requests.get(FEDWATCH_DAILY_URL, timeout=5)
        if resp.status_code != 200:
            log.warning("FedWatch HTTP %s: %s", resp.status_code, resp.text)
            return None
        text = (resp.text or "").strip()
        if not text:
            return None
        if len(text) > 4000:
            text = text[:4000] + "\n\n[macro_context truncated]"
        return text
    except Exception as e:
        log.warning("FedWatch request failed: %s", e)
        return None

# ======================
# OpenAI call
# ======================

def _build_user_payload(snapshot: dict) -> str:
    date_str = datetime.now(BRUSSELS_TZ).strftime("%Y-%m-%d")
    payload = {"date": date_str, "snapshot": snapshot}
    return json.dumps(payload, ensure_ascii=False)


def generate_daily_brief(snapshot: dict) -> str:
    payload_str = _build_user_payload(snapshot)
    model_name = os.getenv("CRYPTOWATCH_DAILY_MODEL", "gpt-4.1-mini")

    try:
        resp = client.chat.completions.create(
            model=model_name,
            temperature=0.7,
            max_tokens=900,
            messages=[
                {"role": "system", "content": DAILY_SYSTEM_PROMPT},
                {"role": "user", "content": "Generate the DAILY brief (no trade plan, no entries/stops/TPs):\n\n" + payload_str},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI call failed: %s", e)
        return (
            "🧠 [CryptoWatch] Daily Macro Brief\n"
            "⚠️ Could not generate full daily analysis today (model error).\n"
            "Manage risk defensively."
        )

# ======================
# Entry point
# ======================

def fetch_fear_greed() -> dict | None:
    """
    Fetch current Fear & Greed index from alternative.me (free, no key).
    Returns {"value": 34, "label": "Fear", "updated": "..."} or None.
    """
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=5,
        )
        data = r.json().get("data", [{}])[0]
        return {
            "value": int(data.get("value", 0)),
            "label": data.get("value_classification", ""),
            "updated": data.get("timestamp", ""),
        }
    except Exception as e:
        log.warning("Fear & Greed fetch failed: %s", e)
        return None


def main():
    try:
        snapshot = fetch_basic_market_snapshot()
    except Exception as e:
        log.exception("error building snapshot: %s", e)
        send_text("🧠 [CryptoWatch] Daily Macro Brief\n⚠️ Could not build market snapshot from Bitget.")
        return

    # Macro context from FedWatch (optional)
    macro_text = fetch_macro_context()
    if macro_text:
        snapshot.setdefault("meta", {})
        snapshot["meta"]["macro_context"] = macro_text

    # Macro overlay from Stooq (optional but preferred)
    try:
        overlay = fetch_macro_overlay()
        if overlay.get("items"):
            snapshot.setdefault("meta", {})
            snapshot["meta"]["macro_overlay"] = overlay
    except Exception as e:
        log.warning("macro overlay fetch failed: %s", e)

    # Fear & Greed index
    try:
        fg = fetch_fear_greed()
        if fg:
            snapshot.setdefault("meta", {})
            snapshot["meta"]["fear_greed"] = fg
    except Exception as e:
        log.warning("fear & greed fetch failed: %s", e)

    brief = generate_daily_brief(snapshot)
    send_text(brief)