import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone
import requests


# ======================
# Config
# ======================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")
BITGET_ENABLED = os.environ.get("BITGET_ENABLED", "0") == "1"
BITGET_POLL_INTERVAL_SEC = int(os.environ.get("BITGET_POLL_INTERVAL_SEC", "15"))
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "")  # e.g. "BTCUSDT"
BITGET_BASE_URL = "https://api.bitget.com"


# ======================
# Helpers
# ======================

def iso_utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method, request_path, params=None, body=None):
    """Bitget V2 authentication."""
    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    # Querystring
    query = ""
    if params:
        from urllib.parse import urlencode
        query = urlencode(params)

    # Body
    body_str = json.dumps(body, separators=(",", ":")) if body else ""

    # Prehash
    if query:
        prehash = timestamp + method + request_path + "?" + query + body_str
        url = f"{BITGET_BASE_URL}{request_path}?{query}"
    else:
        prehash = timestamp + method + request_path + body_str
        url = f"{BITGET_BASE_URL}{request_path}"

    signature = hmac.new(
        BITGET_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256
    ).digest()

    sign_b64 = base64.b64encode(signature).decode()

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

    if method == "GET":
        resp = requests.get(url, headers=headers, timeout=10)
    else:
        resp = reque
