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

# Symbol for MIX futures, e.g. "BTCUSDT"
TRADEWATCH_SYMBOL = os.environ.get("TRADEWATCH_SYMBOL", os.environ.get("BITGET_SYMBOL", ""))

# For MIX futures
TRADEWATCH_MARGIN_COIN = os.environ.get("TRADEWATCH_MARGIN_COIN", "USDT")
TRADEWATCH_PRODUCT_TYPE = os.environ.get("TRADEWATCH_PRODUCT_TYPE", "USDT-FUTURES")

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


def _signed_request(
    method: str,
    request_path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
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


# ======================
# Futures helpers (Bitget MIX)
# ======================

def _fetch_futures_position(symbol: str, margin_coin: str = "USDT") -> dict | None:
    """
    Get current MIX (perpetual futures) position for a symbol.
    Endpoint (v1-style):
        GET /api/mix/v1/position/singlePosition
    """
    params = {
        "symbol": symbol,
        "marginCoin": margin_coin,
    }
    data = _signed_request(
        "GET",
        "/api/mix/v1/position/singlePosition",
        params=params,
        body=None,
    )
    positions = data.get("data") or []
    if not positions:
        return None

    # Hedge mode can have LONG + SHORT, pick the one with non-zero size
    for p in positions:
        hold_side = (p.get("holdSide") or "").lower()  # "long" / "short"
        total = float(p.get("total", "0"))
        if total != 0 and hold_side in {"long", "short"}:
            return p

    return None


def _fetch_futures_plan_orders(symbol: str, product_type: str = "USDT-FUTURES") -> list[dict]:
    """
    Get current MIX plan orders (TP/SL etc.) for a symbol.
    Endpoint:
        GET /api/mix/v1/plan/currentPlan
    """
    params = {
        "symbol": symbol,
        "productType": product_type,
        # "isPlan": "1",  # optional; leave out to get all
    }
    data = _signed_request(
        "GET",
        "/api/mix/v1/plan/currentPlan",
        params=params,
        body=None,
    )
    return data.get("data") or []


def _decode_tp_sl_from_plans(
    symbol: str,
    side: str,             # "long" or "short"
    entry_price: float,
    plans: list[dict],
) -> dict:
    """
    From current plan orders, infer SL and up to 3 TPs for the position.
    Returns:
        {"sl": float|None, "tps": [float, ...], "tp_sizes": [float, ...]}
    """
    tps: list[tuple[float, float]] = []          # (triggerPrice, size)
    sl_candidates: list[tuple[float, float]] = []

    for o in plans:
        if o.get("symbol") != symbol:
            continue

        trigger_price = o.get("triggerPrice")
        size_str = o.get("size") or o.get("quantity") or o.get("sizeDelta") or "0"
        reduce_only = str(o.get("reduceOnly", "")).lower() == "true"
        side_raw = (o.get("side") or "").lower()  # "buy" / "sell"
        if not trigger_price or not reduce_only:
            continue

        tp = float(trigger_price)
        try:
            sz = float(size_str)
        except ValueError:
            sz = 0.0

        # LONG: SL is a sell below entry; TP is sell above
        # SHORT: SL is a buy above entry; TP is buy below
        if side == "long":
            if side_raw == "sell" and tp < entry_price:
                sl_candidates.append((tp, sz))
            elif side_raw == "sell" and tp > entry_price:
                tps.append((tp, sz))
        elif side == "short":
            if side_raw == "buy" and tp > entry_price:
                sl_candidates.append((tp, sz))
            elif side_raw == "buy" and tp < entry_price:
                tps.append((tp, sz))

    sl = None
    if sl_candidates:
        # LONG: highest SL below entry; SHORT: lowest SL above entry
        if side == "long":
            sl = max(x[0] for x in sl_candidates)
        else:
            sl = min(x[0] for x in sl_candidates)

    # sort TPs in execution order (closest first)
    if side == "long":
        tps_sorted = sorted(tps, key=lambda x: x[0])  # low → high
    else:
        # For shorts, TPs are below entry, closest first = highest price first
        tps_sorted = sorted(tps, key=lambda x: x[0], reverse=True)

    tps_prices = [x[0] for x in tps_sorted[:3]]
    tps_sizes = [x[1] for x in tps_sorted[:3]]

    return {"sl": sl, "tps": tps_prices, "tp_sizes": tps_sizes}


def _build_futures_position_snapshot(symbol: str, margin_coin: str = "USDT", product_type: str = "USDT-FUTURES") -> dict | None:
    """
    High-level helper:
      - fetch MIX position
      - fetch plan orders
      - decode TP/SL

    Returns a dict ready to be used for /position or TradeWatch messages.
    """
    pos = _fetch_futures_position(symbol, margin_coin=margin_coin)
    if not pos:
        return None

    entry = float(pos.get("averageOpenPrice") or pos.get("openPriceAvg") or "0")
    size = float(pos.get("total", "0"))
    side = (pos.get("holdSide") or "").lower()   # "long" / "short"
    leverage = pos.get("leverage")
    margin_mode = pos.get("marginMode") or "isolated"
    liq_price = float(pos.get("liquidationPrice", "0") or "0")
    unrealized_pnl = pos.get("unrealizedPL", None)

    plans = _fetch_futures_plan_orders(symbol, product_type=product_type)
    tp_sl = _decode_tp_sl_from_plans(symbol, side, entry, plans)

    snap = {
        "pair": symbol,
        "side": side.upper(),  # LONG / SHORT
        "entry": entry,
        "size": size,
        "leverage": leverage,
        "margin_mode": margin_mode,
        "position_mode": pos.get("holdMode") or "hedge_mode",
        "tps": tp_sl["tps"],           # list of prices
        "tp_sizes": tp_sl["tp_sizes"], # list of sizes
        "sl": tp_sl["sl"],
        "liq_price": liq_price,
        "unrealized_pnl": unrealized_pnl,
    }
    return snap


# ======================
# Fills + formatting
# ======================

def _fetch_futures_fills(limit: int = 50) -> list[dict]:
    """
    Uses Bitget MIX 'Get Fills' endpoint:
        GET /api/mix/v1/order/fills
    Returns list of fill dicts.
    """
    params: dict = {"limit": str(limit)}
    if TRADEWATCH_SYMBOL:
        params["symbol"] = TRADEWATCH_SYMBOL

    data = _signed_request(
        "GET",
        "/api/mix/v1/order/fills",
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


def _format_fill_message(fill: dict) -> str:
    """
    Fallback message format if we can't build a full position snapshot.
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
        f"{side_emoji} [TradeWatch] New Futures Fill\n"
        f"Pair: {pair}\n"
        f"Side: {side_raw}\n"
        f"Price: {entry}\n"
        f"Size: {size}\n"
        f"Execution: {execution}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )


def format_futures_position_message(snap: dict) -> str:
    """
    Turn a futures position snapshot into a nice Telegram message.
    """
    pair = snap["pair"]
    side = snap["side"]
    entry = snap["entry"]
    size = snap["size"]
    lev = snap.get("leverage")
    margin_mode = snap.get("margin_mode", "isolated")
    pos_mode = snap.get("position_mode", "hedge_mode")
    liq = snap.get("liq_price")
    pnl = snap.get("unrealized_pnl")

    tps = snap.get("tps") or []
    sl = snap.get("sl")

    # emoji
    side_emoji = "📉" if side == "SHORT" else "📈"

    lines = [
        f"{side_emoji} Current Futures Position",
        f"Pair: {pair}",
        f"Side: {side}",
        f"Entry Price: {entry}",
        f"Size: {size}",
        f"Leverage: {lev}x",
        f"Margin Mode: {margin_mode}",
        f"Position Mode: {pos_mode}",
        "",
    ]

    # TP lines
    if tps:
        for idx, price in enumerate(tps, start=1):
            lines.append(f"TP{idx}: {price}")
    else:
        lines.append("TP: —")

    # SL
    if sl:
        lines.append(f"SL: {sl}")
    else:
        lines.append("SL: ❌ none")

    lines.extend(
        [
            "",
            f"Liq Price: {liq}",
            f"Unrealized PnL: {pnl}",
            f"Time (UTC): {_iso_utc_now()}",
        ]
    )

    return "\n".join(lines)


# ======================
# Status + main loop
# ======================

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

    Polls Bitget MIX futures fills and sends new trades / position snapshots
    to Telegram using send_func(text).

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

            fills = _fetch_futures_fills(limit=50)

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

                    symbol = f.get("symbol") or TRADEWATCH_SYMBOL

                    # Build futures position snapshot (manual or API)
                    snap = None
                    if symbol:
                        snap = _build_futures_position_snapshot(
                            symbol,
                            margin_coin=TRADEWATCH_MARGIN_COIN,
                            product_type=TRADEWATCH_PRODUCT_TYPE,
                        )

                    if snap:
                        msg = format_futures_position_message(snap)
                    else:
                        # Fallback: just send fill info
                        msg = _format_fill_message(f)

                    send_func(msg)

                # Avoid unbounded growth
                if len(seen) > 3000:
                    seen = set(list(seen)[-2000:])

        except Exception as e:
            STATE["last_error"] = str(e)
            print("[TradeWatch] Error in watcher loop:", e)

        time.sleep(TRADEWATCH_POLL_INTERVAL_SEC)