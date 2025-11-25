import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

import requests

# ======================
# Bitget + TradeWatch config
# ======================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")
BITGET_BASE_URL = "https://api.bitget.com"

# TradeWatch flags
# TRADEWATCH_ENABLED takes priority, but we also respect legacy BITGET_ENABLED.
TRADEWATCH_ENABLED = os.environ.get("TRADEWATCH_ENABLED", os.environ.get("BITGET_ENABLED", "0")) == "1"
TRADEWATCH_POLL_INTERVAL_SEC = int(
    os.environ.get("TRADEWATCH_POLL_INTERVAL_SEC", os.environ.get("BITGET_POLL_INTERVAL_SEC", "15"))
)
TRADEWATCH_SYMBOL = os.environ.get("TRADEWATCH_SYMBOL", os.environ.get("BITGET_SYMBOL", ""))  # e.g. "BTCUSDT" or "" for all

# Simple internal state for /tradewatch_status
STATE = {
    "running": False,
    "last_poll_utc": None,
    "last_trade_utc": None,
    "last_trade_pair": None,
    "last_trade_side": None,
    "last_error": None,
}


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_or_none(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method: str, request_path: str,
                    params: dict | None = None,
                    body: dict | None = None) -> dict:
    """
    Bitget V2 authenticated request.

    Signature format:
    timestamp + method.toUpperCase() + requestPath + "?" + query + body
    """
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

    # Pre-hash string
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

    if resp.status_code != 200:
        raise RuntimeError(f"TradeWatch/Bitget HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"TradeWatch/Bitget API error {data.get('code')}: {data.get('msg')}")
    return data


def _fetch_spot_fills(limit: int = 50) -> list[dict]:
    """
    Uses Bitget Spot V2 'Get Fills' endpoint:
    GET /api/v2/spot/trade/fills

    Returns list of fill dicts.
    """
    params: dict = {"limit": str(limit)}
    if TRADEWATCH_SYMBOL:
        params["symbol"] = TRADEWATCH_SYMBOL

    data = _signed_request(
        "GET",
        "/api/v2/spot/trade/fills",
        params=params,
        body=None,
    )
    return data.get("data", []) or []


def _classify_execution(fill: dict) -> str:
    """
    Best-effort classification based on Bitget fields.
    Will not break if fields are missing.
    """
    scope = (fill.get("tradeScope") or "").lower()
    order_type = (fill.get("orderType") or "").lower()

    # Try to detect TP/SL / close from available hints
    if "take" in scope or "tp" in scope or "take_profit" in scope:
        return "Take Profit"
    if "stop" in scope or "sl" in scope or "stop_loss" in scope:
        return "Stop Loss"

    if "close" in scope or "reduce" in scope or "reduce" in order_type:
        return "Position Close/Reduce"

    return "Position Open/Increase"


def _format_message(fill: dict) -> str:
    """
    Professional message format:

    📈 [TradeWatch] New Position Opened
    Pair: BTCUSDT
    Side: BUY
    Entry Price: 86758
    Size: 0.50
    Execution: Position Open/Increase
    Time (UTC): 2025-11-25 12:00:01
    """
    pair = fill.get("symbol", "N/A")
    side_raw = (fill.get("side") or "N/A").upper()
    entry = fill.get("priceAvg") or fill.get("price") or "N/A"
    size = fill.get("size") or fill.get("amount", "N/A")

    # emoji based on side
    if side_raw == "BUY":
        side_emoji = "📈"
    elif side_raw == "SELL":
        side_emoji = "📉"
    else:
        side_emoji = "📘"

    execution = _classify_execution(fill)

    return (
        f"{side_emoji} [TradeWatch] New Position Executed\n"
        f"Pair: {pair}\n"
        f"Side: {side_raw}\n"
        f"Entry Price: {entry}\n"
        f"Size: {size}\n"
        f"Execution: {execution}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )


def get_status() -> str:
    """
    Return a human-readable status string for /tradewatch_status.
    """
    enabled = TRADEWATCH_ENABLED
    symbol = TRADEWATCH_SYMBOL or "ALL"

    lines = [
        "📈 [TradeWatch] Status",
        f"Enabled: {'Yes ✅' if enabled else 'No ❌'}",
        f"Symbol filter: {symbol}",
        f"Running loop: {'Yes ✅' if STATE['running'] else 'No ❌'}",
        f"Last poll (UTC): {_iso_or_none(STATE['last_poll_utc'])}",
        f"Last trade (UTC): {_iso_or_none(STATE['last_trade_utc'])}",
    ]

    if STATE["last_trade_pair"]:
        lines.append(
            f"Last trade: {STATE['last_trade_pair']} {STATE['last_trade_side'] or ''}".strip()
        )

    if STATE["last_error"]:
        lines.append(f"Last error: {STATE['last_error']}")

    return "\n".join(lines)


def start_tradewatch(send_func):
    """
    TradeWatch main loop.

    Polls Bitget fills and sends new trades to Telegram using send_func(text).
    Use from main.py via a background thread:

        threading.Thread(
            target=start_tradewatch,
            args=(send_text,),
            daemon=True,
        ).start()
    """

    if not TRADEWATCH_ENABLED:
        print("[TradeWatch] Disabled (TRADEWATCH_ENABLED != 1 and BITGET_ENABLED != 1)")
        STATE["running"] = False
        return

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        print("[TradeWatch] Missing Bitget API credentials.")
        STATE["running"] = False
        return

    print("[TradeWatch] Watcher started...")
    STATE["running"] = True

    seen: set[str] = set()
    first_run = True

    while True:
        try:
            STATE["last_poll_utc"] = datetime.now(timezone.utc)

            fills = _fetch_spot_fills(limit=50)

            if first_run:
                # On first run, just mark existing fills as seen (no spam)
                for f in fills:
                    tid = f.get("tradeId")
                    if tid:
                        seen.add(tid)
                first_run = False
            else:
                # Assume newest first; send oldest unseen first
                for f in reversed(fills):
                    tid = f.get("tradeId")
                    if not tid or tid in seen:
                        continue

                    seen.add(tid)

                    # Update status
                    STATE["last_trade_utc"] = datetime.now(timezone.utc)
                    STATE["last_trade_pair"] = f.get("symbol")
                    STATE["last_trade_side"] = (f.get("side") or "").upper()
                    STATE["last_error"] = None

                    msg = _format_message(f)
                    send_func(msg)

                # Avoid unbounded growth
                if len(seen) > 3000:
                    seen = set(list(seen)[-2000:])

        except Exception as e:
            STATE["last_error"] = str(e)
            print("[TradeWatch] Error in watcher loop:", e)

        time.sleep(TRADEWATCH_POLL_INTERVAL_SEC)
