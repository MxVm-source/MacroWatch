import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

import requests

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")
BITGET_ENABLED = os.environ.get("BITGET_ENABLED", "0") == "1"
BITGET_POLL_INTERVAL_SEC = int(os.environ.get("BITGET_POLL_INTERVAL_SEC", "15"))
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "")  # e.g. "BTCUSDT"
BITGET_BASE_URL = "https://api.bitget.com"


def _iso_utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method, request_path, params=None, body=None):
    """Bitget V2 authenticated request."""
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise RuntimeError("Bitget API credentials not set")

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
    sig = hmac.new(
        BITGET_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign_b64 = base64.b64encode(sig).decode()

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

    data = resp.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget API error: {data}")
    return data


def _fetch_spot_fills(limit=50):
    """GET /api/v2/spot/trade/fills"""
    params = {"limit": str(limit)}
    if BITGET_SYMBOL:
        params["symbol"] = BITGET_SYMBOL

    data = _signed_request(
        "GET",
        "/api/v2/spot/trade/fills",
        params=params,
    )
    return data.get("data", [])


def _format_message(fill):
    pair = fill.get("symbol", "N/A")
    side = (fill.get("side") or "N/A").upper()
    entry = fill.get("priceAvg") or fill.get("price") or "N/A"
    size = fill.get("size") or fill.get("amount", "N/A")

    return (
        "📘 New Position Opened\n"
        f"Pair: {pair}\n"
        f"Side: {side}\n"
        f"Entry Price: {entry}\n"
        f"Size: {size}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )


def start_bitget_watcher(send_func):
    """
    Blocking loop. Call this in a background thread:
    threading.Thread(target=start_bitget_watcher, args=(send_text,), daemon=True).start()
    """

    if not BITGET_ENABLED:
        print("[Bitget] Disabled (BITGET_ENABLED != 1)")
        return

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        print("[Bitget] Missing API credentials.")
        return

    print("[Bitget] Watcher started...")

    seen = set()
    first = True

    while True:
        try:
            fills = _fetch_spot_fills(limit=50)

            if first:
                # Mark existing fills as seen, no spam on first boot
                for f in fills:
                    tid = f.get("tradeId")
                    if tid:
                        seen.add(tid)
                first = False
            else:
                # Assume newest first; send oldest unseen first
                for f in reversed(fills):
                    tid = f.get("tradeId")
                    if not tid or tid in seen:
                        continue

                    seen.add(tid)
                    msg = _format_message(f)
                    send_func(msg)

                if len(seen) > 3000:
                    seen = set(list(seen)[-2000:])

        except Exception as e:
            print("[Bitget] Error:", e)

        time.sleep(BITGET_POLL_INTERVAL_SEC)
