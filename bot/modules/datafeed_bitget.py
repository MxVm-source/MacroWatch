import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

import requests

# ======================
# Shared Bitget config
# ======================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")
BITGET_BASE_URL = "https://api.bitget.com"

# Futures defaults (USDT-M)
BITGET_PRODUCT_TYPE = os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
BITGET_MARGIN_COIN = os.environ.get("BITGET_MARGIN_COIN", "USDT")
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "BTCUSDT")  # your main futures pair


def iso_utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method: str, request_path: str, params: dict | None = None, body: dict | None = None):
    """Generic Bitget private request helper (V2)."""
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise RuntimeError("Bitget API credentials not set")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    query = ""
    if params:
        from urllib.parse import urlencode
        query = urlencode(params)

    body_str = json.dumps(body, separators=(",", ":")) if body else ""

    if query:
        prehash = timestamp + method + request_path + "?" + query + body_str
        url = f"{BITGET_BASE_URL}{request_path}?{query}"
    else:
        prehash = timestamp + method + request_path + body_str
        url = f"{BITGET_BASE_URL}{request_path}"

    sign_bytes = hmac.new(
        BITGET_API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
    ).digest()
    sign_b64 = base64.b64encode(sign_bytes).decode()

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=10)
    else:
        resp = requests.post(url, headers=headers, data=body_str, timeout=10)

    if resp.status_code != 200:
        raise RuntimeError(f"Bitget HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget API error {data.get('code')}: {data.get('msg')}")
    return data