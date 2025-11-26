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
BITGET_PRODUCT_TYPE = os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
BITGET_MARGIN_COIN = os.environ.get("BITGET_MARGIN_COIN", "USDT")
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "BTCUSDT")

# ======================
# Helpers
# ======================

def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _signed_request(method: str, request_path: str,
                    params: dict | None = None,
                    body: dict | None = None):

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

    sign = hmac.new(
        BITGET_API_SECRET.encode(),
        prehash.encode(),
        hashlib.sha256
    ).digest()

    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": base64.b64encode(sign).decode(),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
    }

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=10)
    else:
        r = requests.post(url, headers=headers, data=body_str, timeout=10)

    if r.status_code != 200:
        raise RuntimeError(f"Bitget HTTP {r.status_code}: {r.text}")

    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget API error {data.get('code')}: {data.get('msg')}")

    return data

# ======================
# Fetch Futures Data
# ======================

def _fetch_current_futures_position():
    """
    Returns a single futures position dict or None.
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


def _fetch_pending_tp_sl_orders():
    """
    Fetch TP and SL trigger orders.
    Returns {"tp": [...], "sl": [...]}
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

    tps, sls = [], []

    for o in entrusted:
        if o.get("symbol", "").upper() != BITGET_SYMBOL.upper():
            continue

        # TP
        tp = o.get("stopSurplusTriggerPrice")
        if tp:
            try:
                tps.append(float(tp))
            except:
                pass

        # SL
        sl = o.get("stopLossTriggerPrice")
        if sl:
            try:
                sls.append(float(sl))
            except:
                pass

    return {"tp": tps, "sl": sls}

# ======================
# Main Build Function
# ======================

def build_futures_position_message() -> str:
    pos = _fetch_current_futures_position()

    if not pos or float(pos.get("total", "0") or "0") == 0.0:
        return f"ℹ️ No open futures position for {BITGET_SYMBOL}."

    side_raw = (pos.get("holdSide") or "").lower()
    if side_raw == "long":
        side = "LONG"
    elif side_raw == "short":
        side = "SHORT"
    else:
        side = side_raw.upper()

    entry = pos.get("openPriceAvg") or pos.get("openPrice") or "N/A"
    size = pos.get("total") or pos.get("available") or "N/A"
    lev = pos.get("leverage") or "N/A"
    liq = pos.get("liquidationPrice") or pos.get("liqPx") or "N/A"
    pnl = pos.get("unrealizedPL") or pos.get("upl") or "0"
    margin_mode = pos.get("marginMode") or ""
    pos_mode = pos.get("posMode") or ""

    triggers = _fetch_pending_tp_sl_orders()
    tp_prices = triggers["tp"]
    sl_prices = triggers["sl"]

    reverse = True if side == "SHORT" else False
    tp_sorted = sorted(tp_prices, reverse=reverse)
    sl_sorted = sorted(sl_prices, reverse=reverse)

    lines = [
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

    # TP block
    if tp_sorted:
        lines.append("")
        for idx, p in enumerate(tp_sorted[:3], start=1):
            lines.append(f"TP{idx}: {p}")

    # SL block
    if sl_sorted:
        lines.append("")
        lines.append(f"SL: {sl_sorted[0]}")

    if liq and liq != "0":
        lines.append(f"Liq Price: {liq}")
    if pnl is not None:
        lines.append(f"Unrealized PnL: {pnl}")

    lines.append(f"Time (UTC): {iso_utc_now()}")

    return "\n".join(lines)

# ======================
# Safe Wrapper
# ======================

def get_position_report_safe() -> str:
    try:
        return build_futures_position_message()
    except Exception as e:
        print("[Bitget] /position error:", e)
        return "⚠️ Could not fetch position from Bitget. Check API keys & futures permission settings."