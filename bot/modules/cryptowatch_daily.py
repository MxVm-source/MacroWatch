import logging
import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

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
# Stooq macro overlay symbols (env override supported)
# ======================

# Stooq daily history endpoint:
# https://stooq.com/q/d/l/?s=^spx&i=d
STOOQ_BASE = "https://stooq.com/q/d/l/"

# Defaults (you can override in Render env vars if needed)
STOOQ_SYMBOL_DXY   = os.getenv("STOOQ_SYMBOL_DXY", "^dxy").strip()     # Dollar Index
STOOQ_SYMBOL_SPX   = os.getenv("STOOQ_SYMBOL_SPX", "^spx").strip()     # S&P 500 Index
STOOQ_SYMBOL_GOLD  = os.getenv("STOOQ_SYMBOL_GOLD", "xauusd").strip()  # Gold spot proxy
STOOQ_SYMBOL_US10Y = os.getenv("STOOQ_SYMBOL_US10Y", "^tnx").strip()   # 10Y yield proxy (TNX)

# ======================
# OpenAI prompt
# ======================

DAILY_SYSTEM_PROMPT = """You are CryptoWatch, a desk analyst sending a pre-US-open snapshot to ETH/BTC perp traders.

HARD RULES:
- 150-200 words MAX. If you exceed 200 words you have failed.
- No intro sentences. No "today we see" or "as we head into". Start with data.
- Every sentence must contain a number or a directional signal. Cut the rest.
- Use ONLY prices from the live snapshot. Never invent or estimate prices.
- No entries, stops, or TP targets.
- Never say you are an AI.

OUTPUT FORMAT (follow exactly, no extra sections):

📊 [CryptoWatch] {date} — Pre-US Open

Mood: [Bullish/Bearish/Neutral/Cautious] — [one clause, max 10 words]
Fear & Greed: [value]/100 — [label] (only include if fear_greed is in snapshot)

BTC  $[lastPr] | 24h [change%] | H [high24h] / L [low24h]
ETH  $[lastPr] | 24h [change%] | [outperformed / underperformed BTC]
Funding: BTC [positive/negative/neutral] / ETH [positive/negative/neutral]

Macro:
• DXY [↑/↓/→] [value] — [one crypto implication, max 8 words]
• US10Y [↑/↓/→] [value] — [one implication, max 8 words]
• SPX [↑/↓/→] [value] — [risk-on/risk-off, max 6 words]

Bias: [Bullish/Bearish/Neutral/Choppy]
Watch: [one level or event, max 12 words]
ETH Watch: [one ETH-specific level or signal, max 12 words]
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

def _stooq_fetch_last_two_daily(symbol: str) -> list[dict]:
    """
    Fetch last 2 daily rows from Stooq CSV.
    Returns list of dict rows: [{"date": "...", "close": float}, ...] newest last.
    """
    sym = (symbol or "").strip().lower()
    if not sym:
        return []

    # Stooq expects e.g. ^spx, xauusd, ^dxy, ^tnx
    url = f"{STOOQ_BASE}?s={quote(sym)}&i=d"
    try:
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            return []
        text = (r.text or "").strip()
        if not text or "404" in text.lower():
            return []
        lines = text.splitlines()
        if len(lines) < 3:
            return []

        # header: Date,Open,High,Low,Close,Volume
        # grab last two data lines
        data_lines = [ln for ln in lines[1:] if ln and "," in ln][-2:]
        out = []
        for ln in data_lines:
            parts = ln.split(",")
            if len(parts) < 5:
                continue
            dt = parts[0].strip()
            close_s = parts[4].strip()
            try:
                close = float(close_s)
            except Exception:
                continue
            out.append({"date": dt, "close": close})
        return out
    except Exception:
        return []


def _macro_point(name: str, symbol: str) -> dict | None:
    rows = _stooq_fetch_last_two_daily(symbol)
    if not rows:
        return None

    last = rows[-1]["close"]
    prev = rows[-2]["close"] if len(rows) >= 2 else None

    change = None
    change_pct = None
    direction = "flat"

    if prev and prev != 0:
        change = last - prev
        change_pct = (change / prev) * 100.0
        if abs(change_pct) < 0.05:
            direction = "flat"
        elif change > 0:
            direction = "up"
        else:
            direction = "down"

    return {
        "name": name,
        "symbol": symbol,
        "last": last,
        "prev_close": prev,
        "change": change,
        "change_pct": change_pct,
        "direction": direction,
        "as_of": rows[-1]["date"],
    }


def fetch_macro_overlay() -> dict:
    """
    Returns macro overlay dict. Missing instruments are simply omitted.
    """
    overlay = {"source": "stooq", "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    items = []

    # US10Y via TNX (index points; narrative still works: up/down yields)
    us10y = _macro_point("US10Y (TNX proxy)", STOOQ_SYMBOL_US10Y)
    if us10y:
        items.append(us10y)

    dxy = _macro_point("DXY", STOOQ_SYMBOL_DXY)
    if dxy:
        items.append(dxy)

    spx = _macro_point("S&P 500", STOOQ_SYMBOL_SPX)
    if spx:
        items.append(spx)

    gold = _macro_point("Gold", STOOQ_SYMBOL_GOLD)
    if gold:
        items.append(gold)

    overlay["items"] = items
    overlay["notes"] = "Daily close-to-close changes. US10Y uses TNX proxy where available."
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