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

# Backward compatible single symbol (used as fallback / default)
BITGET_SYMBOL = os.environ.get("BITGET_SYMBOL", "BTCUSDT").strip().upper()

# Multi-symbol support (comma-separated). Default: BTC + ETH
# Example: BTCUSDT,ETHUSDT
BITGET_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("BITGET_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    if s.strip()
]
if not BITGET_SYMBOLS:
    BITGET_SYMBOLS = [BITGET_SYMBOL]

# ======================
# Helpers
# ======================

def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return f"🟢 {pnl}"
    elif pnl < 0:
        return f"🔴 {pnl}"
    return f"⚪ {pnl}"


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


def get_ticker(symbol: str):
    """
    Public Bitget futures ticker helper.
    Used by FedWatch to measure BTC/ETH reaction.

    Returns last price as float, or None on error.
    """
    try:
        url = f"{BITGET_BASE_URL}/api/v2/mix/market/ticker"
        resp = requests.get(url, params={"symbol": symbol}, timeout=5)
        data = resp.json()

        if data.get("code") != "00000":
            print("[Bitget] get_ticker error:", data)
            return None

        tick = data.get("data") or {}
        # Sometimes Bitget returns a list
        if isinstance(tick, list):
            tick = tick[0] if tick else {}

        price_str = (
            tick.get("last")
            or tick.get("close")
            or tick.get("markPrice")
        )

        if not price_str:
            return None

        return float(price_str)
    except Exception as e:
        print("[Bitget] get_ticker exception:", e)
        return None


# ======================
# Fetch Futures Data
# ======================

def _fetch_current_futures_position(symbol: str):
    params = {
        "productType": BITGET_PRODUCT_TYPE,
        "symbol": symbol,
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


def _fetch_pending_tp_sl_orders(symbol: str):
    params = {
        "planType": "profit_loss",
        "productType": BITGET_PRODUCT_TYPE,
        "symbol": symbol,
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
        if o.get("symbol", "").upper() != symbol.upper():
            continue

        tp = o.get("stopSurplusTriggerPrice")
        if tp:
            try:
                tps.append(float(tp))
            except:
                pass

        sl = o.get("stopLossTriggerPrice")
        if sl:
            try:
                sls.append(float(sl))
            except:
                pass

    return {"tp": tps, "sl": sls}


def _position_is_open(pos: dict | None) -> bool:
    if not pos:
        return False
    try:
        return float(pos.get("total", "0") or "0") != 0.0
    except:
        return False


# ======================
# Build Position Message
# ======================

def build_futures_position_message(symbol: str | None = None) -> str:
    """
    Single-symbol position report (kept for backward compatibility).
    If symbol is None, uses BITGET_SYMBOL.
    """
    symbol = (symbol or BITGET_SYMBOL).upper()
    pos = _fetch_current_futures_position(symbol)

    if not _position_is_open(pos):
        return f"ℹ️ No open futures position for {symbol}."

    side_raw = (pos.get("holdSide") or "").lower()
    side = "LONG" if side_raw == "long" else "SHORT"

    entry = pos.get("openPriceAvg") or pos.get("openPrice") or "N/A"
    size = pos.get("total") or pos.get("available") or "N/A"
    lev = pos.get("leverage") or "N/A"
    liq = pos.get("liquidationPrice") or pos.get("liqPx") or "N/A"
    pnl = pos.get("unrealizedPL") or pos.get("upl") or "0"

    margin_mode = pos.get("marginMode") or ""
    pos_mode = pos.get("posMode") or ""

    triggers = _fetch_pending_tp_sl_orders(symbol)
    tp_sorted = sorted(triggers["tp"], reverse=(side == "SHORT"))
    sl_sorted = sorted(triggers["sl"], reverse=(side == "SHORT"))

    lines = [
        "📘 Current Futures Position",
        f"Pair: {symbol}",
        f"Side: {side}",
        f"Entry Price: {entry}",
        f"Size: {size}",
        f"Leverage: {lev}x",
    ]

    if margin_mode:
        lines.append(f"Margin Mode: {margin_mode}")
    if pos_mode:
        lines.append(f"Position Mode: {pos_mode}")

    if tp_sorted:
        lines.append("")
        for i, p in enumerate(tp_sorted[:3], 1):
            lines.append(f"TP{i}: {p}")

    if sl_sorted:
        lines.append("")
        lines.append(f"SL: {sl_sorted[0]}")

    # RR Ratio
    if tp_sorted and sl_sorted:
        try:
            entry_f = float(entry)
            tp = tp_sorted[0]
            sl = sl_sorted[0]

            if side == "LONG":
                risk = entry_f - sl
                reward = tp - entry_f
            else:
                risk = sl - entry_f
                reward = entry_f - tp

            if risk > 0:
                rr = reward / risk
                lines.append(f"RR Ratio: {rr:.2f}")
        except:
            pass

    if liq:
        lines.append(f"Liq Price: {liq}")

    try:
        pnl_f = float(pnl)
        lines.append(f"Unrealized PnL: {_pnl_color(pnl_f)}")
    except:
        lines.append(f"Unrealized PnL: {pnl}")

    lines.append(f"Time (UTC): {iso_utc_now()}")

    return "\n".join(lines)


def build_multi_futures_position_message(symbols: list[str] | None = None) -> str:
    """
    Multi-symbol position report.
    - If no positions are open: returns ONE clean 'no open positions' line.
    - If at least one is open: returns ONLY open position reports (no spam for flat symbols).
    """
    symbols = symbols or BITGET_SYMBOLS

    open_reports: list[str] = []

    for sym in symbols:
        sym_u = sym.strip().upper()
        if not sym_u:
            continue
        pos = _fetch_current_futures_position(sym_u)
        if _position_is_open(pos):
            open_reports.append(build_futures_position_message(sym_u))

    if not open_reports:
        return f"ℹ️ No open futures positions for: {', '.join(symbols)}."

    return "\n\n".join(open_reports)


def get_position_report_safe() -> str:
    """
    Safe wrapper used by modules.
    Now returns multi-symbol report by default.
    """
    try:
        return build_multi_futures_position_message()
    except Exception as e:
        print("[Bitget] /position error:", e)
        return "⚠️ Could not fetch position from Bitget. Check API keys & futures permissions."