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
TRADEWATCH_POLL_INTERVAL_SEC = int(os.environ.get("TRADEWATCH_POLL_INTERVAL_SEC", "10"))
TRADEWATCH_SYMBOL = os.environ.get("TRADEWATCH_SYMBOL", "")   # e.g. "BTCUSDT" or blank for all


STATE = {
    "running": False,
    "last_poll_utc": None,
    "last_trade_utc": None,
    "last_trade_pair": None,
    "last_trade_side": None,
    "last_error": None,
}


# =========================
# Helpers
# =========================

def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_or_none(dt):
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method: str, request_path: str,
                    params: dict | None = None,
                    body: dict | None = None):
    """
    Generic Bitget signed request (V2).
    """
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise RuntimeError("TradeWatch: Bitget API credentials not set.")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    query = ""
    if params:
        from urllib.parse import urlencode
        query = urlencode(params)

    body_str = json.dumps(body, separators=(",", ":")) if body else ""

    # Prehash string
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
        raise RuntimeError(f"TradeWatch HTTP {r.status_code}: {r.text}")

    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"TradeWatch API error {data.get('code')}: {data.get('msg')}")

    return data


# =========================
# Fetch Spot Trade Fills
# =========================

def _fetch_spot_fills(limit: int = 50) -> list[dict]:
    """
    Uses Bitget SPOT V2: GET /api/v2/spot/trade/fills
    Only admin's trades (your account) are returned.
    """
    params = {"limit": str(limit)}

    if TRADEWATCH_SYMBOL:
        params["symbol"] = TRADEWATCH_SYMBOL

    data = _signed_request(
        "GET",
        "/api/v2/spot/trade/fills",
        params=params,
        body=None,
    )
    return data.get("data", []) or []


# =========================
# Classification Logic
# =========================

def _classify_execution(fill: dict) -> str:
    """
    Classify if execution is:
    - Take Profit
    - Stop Loss
    - Position Close/Reduce
    - Position Open/Increase
    """
    scope = (fill.get("tradeScope") or "").lower()
    order_type = (fill.get("orderType") or "").lower()

    if "take" in scope or "tp" in scope:
        return "Take Profit"
    if "stop" in scope or "sl" in scope:
        return "Stop Loss"
    if "close" in scope or "reduce" in scope or "reduce" in order_type:
        return "Position Close/Reduce"

    return "Position Open/Increase"


# =========================
# Formatting the Telegram Message
# =========================

def _format_message(fill: dict) -> str:
    """
    TradeWatch Telegram output.
    Only admin trades (your Bitget account fills) appear here.
    """
    pair = fill.get("symbol", "N/A")
    side_raw = (fill.get("side") or "N/A").upper()
    price = fill.get("priceAvg") or fill.get("price") or "N/A"
    size = fill.get("size") or fill.get("amount") or "N/A"

    # Side-based emoji
    if side_raw == "BUY":
        emoji = "📈"
    elif side_raw == "SELL":
        emoji = "📉"
    else:
        emoji = "📘"

    execution_type = _classify_execution(fill)

    return (
        f"{emoji} [TradeWatch] Admin Trade Executed\n"
        f"Pair: {pair}\n"
        f"Side: {side_raw}\n"
        f"Price: {price}\n"
        f"Size: {size}\n"
        f"Execution: {execution_type}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )


# =========================
# Command: Status Report
# =========================

def get_status() -> str:
    """
    Used by /tradewatch_status
    """
    symbol = TRADEWATCH_SYMBOL or "ALL"

    lines = [
        "📈 [TradeWatch] Status",
        f"Enabled: {'Yes ✅' if TRADEWATCH_ENABLED else 'No ❌'}",
        f"Symbol Filter: {symbol}",
        f"Running Loop: {'Yes ✅' if STATE['running'] else 'No ❌'}",
        f"Last Poll (UTC): {_iso_or_none(STATE['last_poll_utc'])}",
        f"Last Trade (UTC): {_iso_or_none(STATE['last_trade_utc'])}",
    ]

    if STATE["last_trade_pair"]:
        lines.append(
            f"Last Trade: {STATE['last_trade_pair']} {STATE['last_trade_side']}"
        )

    if STATE["last_error"]:
        lines.append(f"Last Error: {STATE['last_error']}")

    return "\n".join(lines)


# =========================
# Main Loop
# =========================

def start_tradewatch(send_func):
    """
    Start the TradeWatch loop. Call in main.py via:

        threading.Thread(
            target=start_tradewatch,
            args=(send_text,),
            daemon=True,
        ).start()
    """
    if not TRADEWATCH_ENABLED:
        print("[TradeWatch] Disabled (TRADEWATCH_ENABLED != 1)")
        STATE["running"] = False
        return

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        print("[TradeWatch] Missing Bitget API credentials.")
        STATE["running"] = False
        return

    print("[TradeWatch] Watcher started.")
    STATE["running"] = True

    seen = set()
    first_run = True

    while True:
        try:
            STATE["last_poll_utc"] = datetime.now(timezone.utc)

            fills = _fetch_spot_fills(limit=50)

            if first_run:
                # Do NOT spam old trades at startup
                for f in fills:
                    tid = f.get("tradeId")
                    if tid:
                        seen.add(tid)
                first_run = False
            else:
                # Process newest unseen trades (oldest first)
                for f in reversed(fills):
                    tid = f.get("tradeId")
                    if not tid or tid in seen:
                        continue

                    seen.add(tid)

                    # Update state
                    STATE["last_trade_utc"] = datetime.now(timezone.utc)
                    STATE["last_trade_pair"] = f.get("symbol")
                    STATE["last_trade_side"] = (f.get("side") or "?").upper()
                    STATE["last_error"] = None

                    msg = _format_message(f)
                    send_func(msg)

                # Trim memory if needed
                if len(seen) > 3000:
                    seen = set(list(seen)[-2000:])

        except Exception as e:
            STATE["last_error"] = str(e)
            print("[TradeWatch ERROR]:", e)

        time.sleep(TRADEWATCH_POLL_INTERVAL_SEC)