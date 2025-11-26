import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

import requests

# =========================
# TradeWatch Configuration
# =========================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

BITGET_BASE_URL = "https://api.bitget.com"

TRADEWATCH_ENABLED = os.environ.get("TRADEWATCH_ENABLED", "0") == "1"
TRADEWATCH_DEGEN = os.environ.get("TRADEWATCH_DEGEN", "0") == "1"
TRADEWATCH_SYMBOL = os.environ.get("TRADEWATCH_SYMBOL", "")  # optional filter
TRADEWATCH_POLL_INTERVAL_SEC = int(os.environ.get("TRADEWATCH_POLL_INTERVAL_SEC", "10"))

STATE = {
    "running": False,
    "last_poll_utc": None,
    "last_trade_utc": None,
    "last_trade_pair": None,
    "last_trade_side": None,
    "last_error": None,
}

# Degen templates
DEGEN_OPEN = [
    "🚀 Admin yeeted into a trade!",
    "💀 Admin just sent it.",
    "🧨 Admin deployed capital irresponsibly.",
    "🔥 Position opened — cope accordingly.",
    "🦍 Big ape energy detected.",
]

DEGEN_CLOSE = [
    "💼 Trade closed — consequences unknown.",
    "📉 Exit deployed (survived… barely).",
    "🪦 Position closed — funeral avoided.",
    "💸 Trade ended — PnL prayed for.",
]

# =========================
# Helpers
# =========================

def _iso_utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _iso_or_none(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"

def _signed_request(method, request_path, params=None, body=None):
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise RuntimeError("TradeWatch: Bitget API credentials missing")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))
    query = ""

    if params:
        from urllib.parse import urlencode
        query = urlencode(params)

    body_str = json.dumps(body, separators=(",",":")) if body else ""

    if query:
        prehash = timestamp + method + request_path + "?" + query + body_str
        url = f"{BITGET_BASE_URL}{request_path}?{query}"
    else:
        prehash = timestamp + method + request_path + body_str
        url = f"{BITGET_BASE_URL}{request_path}"

    sig = hmac.new(
        BITGET_API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
    ).digest()

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": base64.b64encode(sig).decode(),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=10)
    else:
        r = requests.post(url, headers=headers, data=body_str, timeout=10)

    if r.status_code != 200:
        raise RuntimeError(f"TradeWatch HTTP {r