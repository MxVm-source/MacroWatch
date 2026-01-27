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

log.warning("✅ cryptowatch_daily.py LOADED (OpenAI version)")

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
# OpenAI prompt
# ======================

DAILY_SYSTEM_PROMPT = """You are CryptoWatch, an elite crypto market analyst writing a concise, trader-focused daily brief.
[... keep your prompt exactly as-is ...]
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

    snapshot["meta"]["total_market_cap"] = None
    snapshot["meta"]["notes"] = "BTC/ETH USDT perpetual futures data from Bitget V2 (ticker)."
    snapshot["meta"]["macro_context"] = None

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
                {"role": "user", "content": "Generate the DAILY market brief ONLY (no strategy plan):\n\n" + payload_str},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("OpenAI call failed: %s", e)
        return (
            "🧠 [CryptoWatch] AI Market Brief\n"
            "⚠️ Could not generate full daily analysis today (model error).\n"
            "Assume conditions are uncertain and manage risk defensively."
        )

# ======================
# Entry point
# ======================

def main():
    try:
        snapshot = fetch_basic_market_snapshot()
    except Exception as e:
        log.exception("error building snapshot: %s", e)
        send_text("🧠 [CryptoWatch] AI Market Brief\n⚠️ Could not build market snapshot from Bitget.")
        return

    macro_text = fetch_macro_context()
    if macro_text:
        snapshot.setdefault("meta", {})
        snapshot["meta"]["macro_context"] = macro_text

    brief = generate_daily_brief(snapshot)
    send_text(brief)