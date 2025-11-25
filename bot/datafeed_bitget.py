import os
import time
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

import requests

# ======================
# Bitget config
# ======================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")

BITGET_BASE_URL = "https://api.bitget.com"

# Futures defaults (USDT-M)
# You can override these in Render env vars if needed
BITGET_PRODUCT_TYPE = os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
BITGET_MARGIN_COIN = os.environ.get("BITGET_MARGIN_COIN", "USDT")
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "BTCUSDT")  # main perp you trade


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method: str, request_path: str,
                    params: dict | None = None,
                    body: dict | None = None):
    """
    Generic Bitget private request helper (V2).
    Signature: timestamp + method + requestPath + ?query + body
    """
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
        BITGET_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
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


# ======================
# Futures position + TP
# ======================

def _fetch_current_futures_position():
    """
    Futures: GET /api/v2/mix/position/single-position
    Returns the first position dict or None if no position.
    """
    params = {
        "productType": BITGET_PRODUCT_TYPE,
        "symbol": BITGET_SYMBOL,
        "marginCoin": BITGET_MARGIN_COIN,
    }
    res = _signed_request(
        "GET",
        "/api/v2/mix/position/single-position",
        params=params,
        body=None,
    )
    data = res.get("data") or []
    return data[0] if data else None


def _fetch_pending_tp_orders():
    """
    Futures TP/SL trigger orders:
    GET /api/v2/mix/order/orders-plan-pending
    planType=profit_loss → returns TP/SL orders.
    We filter to TPs for the current symbol.
    """
    params = {
        "planType": "profit_loss",
        "productType": BITGET_PRODUCT_TYPE,
        "symbol": BITGET_SYMBOL,
        "limit": "100",
    }
    res = _signed_request(
        "GET",
        "/api/v2/mix/order/orders-plan-pending",
        params=params,
        body=None,
    )
    data = res.get("data") or {}
    entrusted = data.get("entrustedList") or []
    tps: list[float] = []

    for o in entrusted:
        # Bitget uses stopSurplusTriggerPrice for TP
        tp_price = o.get("stopSurplusTriggerPrice") or ""
        if not tp_price:
            continue

        if o.get("symbol", "").upper() != BITGET_SYMBOL.upper():
            continue

        try:
            price_f = float(tp_price)
        except ValueError:
            continue

        tps.append(price_f)

    return tps


def build_futures_position_message() -> str:
    """
    Returns a clean, professional snapshot of current futures position
    + up to 3 TP levels (from trigger orders).
    """
    pos = _fetch_current_futures_position()
    if not pos or float(pos.get("total", "0") or "0") == 0.0:
        return f"ℹ️ No open futures position for {BITGET_SYMBOL}."

    side_raw = (pos.get("holdSide") or "").lower()
    if side_raw == "long":
        side = "LONG"
    elif side_raw == "short":
        side = "SHORT"
    else:
        side = side_raw.upper() or "N/A"

    entry = pos.get("openPriceAvg") or pos.get("openPrice") or "N/A"
    size = pos.get("total") or pos.get("available") or "N/A"
    lev = pos.get("leverage") or "N/A"
    liq = pos.get("liquidationPrice") or pos.get("liqPx") or "N/A"
    pnl = pos.get("unrealizedPL") or pos.get("upl") or "0"
    margin_mode = pos.get("marginMode") or ""
    pos_mode = pos.get("posMode") or ""

    tp_prices = _fetch_pending_tp_orders()
    # LONG: sort low → high, SHORT: high → low
    reverse = True if side == "SHORT" else False
    tp_prices_sorted = sorted(tp_prices, reverse=reverse)
    tp_lines = []
    for idx, p in enumerate(tp_prices_sorted[:3], start=1):
        tp_lines.append(f"TP{idx}: {p}")

    lines: list[str] = [
        "📘 Current Futures Position",
        f"Pair: {BITGET_SYMBOL}",
        f"Side: {side}",
        f"Entry Price: {entry}",
        f"Size: {size}",
        f"Leverage: {lev}x",
    ]

    if margin_mode:
        lines.append(f"Margin Mode: {margin_mode}")
    if pos_mode:
        lines.append(f"Position Mode: {pos_mode}")

    if tp_lines:
        lines.append("")  # blank line before TP block
        lines.extend(tp_lines)

    if liq and liq != "0":
        lines.append(f"Liq Price: {liq}")
    if pnl is not None:
        lines.append(f"Unrealized PnL: {pnl}")

    lines.append(f"Time (UTC): {iso_utc_now()}")

    return "\n".join(lines)


def get_position_report_safe() -> str:
    """
    Wrapper you call from MacroWatch.
    Never crashes the bot – returns a friendly error instead.
    """
    try:
        return build_futures_position_message()
    except Exception as e:
        print("[Bitget] /position error:", e)
        return "⚠️ Could not fetch position from Bitget. Check API keys & futures permissions."
