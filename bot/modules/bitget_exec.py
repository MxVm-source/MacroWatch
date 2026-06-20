# bot/modules/bitget_exec.py
"""
bitget_exec.py — the ONLY module that places/cancels/modifies orders.

Everything else in MacroWatch is read-only/observational. This is the first
active component, so it is deliberately small, explicit, and gated.

SAFETY GATES (both must be satisfied for a real order to leave the process):
  1. STAGE_LIVE env  != "true"   -> dry-run: log the exact payload, send nothing.
  2. Elite credentials present   -> otherwise raises, never silently no-ops.

Account: discretionary book lives on the ELITE account, so every call here
signs with _signed_request_elite. The main/ATRb account is never touched.

Bitget v2 schema (verified against api-doc, 2026-06):
  entry      POST /api/v2/mix/order/place-order
  ladder TP  POST /api/v2/mix/order/place-tpsl-order   (planType=profit_plan, has size)
  pos SL     POST /api/v2/mix/order/place-pos-tpsl      (whole-position stop -> ratchet)
  cancel     POST /api/v2/mix/order/cancel-plan-order   (VERIFY param shape on your acct)
  flatten    POST /api/v2/mix/order/close-positions     (VERIFY on your acct)

side mapping:  LONG -> side=buy   SHORT -> side=sell    (tradeSide=open)
holdSide:      LONG -> long       SHORT -> short
"""

import os
import time
import logging

import requests

from bot.datafeed_bitget import (
    _signed_request_elite,
    BITGET_PRODUCT_TYPE,
    BITGET_MARGIN_COIN,
    ELITE_API_KEY,
)

log = logging.getLogger("bitget_exec")

# ─── Gates / config ──────────────────────────────────────────────────────────
STAGE_LIVE   = os.getenv("STAGE_LIVE", "false").lower() in ("1", "true", "yes", "on")
MARGIN_MODE  = os.getenv("BITGET_MARGIN_MODE", "isolated")   # isolated | crossed
# one-way: tradeSide is ignored by Bitget. hedge: tradeSide=open required.
ONE_WAY_MODE = os.getenv("BITGET_ONE_WAY", "true").lower() in ("1", "true", "yes", "on")
SIZE_DECIMALS = int(os.getenv("BITGET_SIZE_DECIMALS", "3"))  # BTC contract step; VERIFY per symbol


def is_live() -> bool:
    return STAGE_LIVE and bool(ELITE_API_KEY)


def _round_size(qty: float) -> str:
    step = 10 ** (-SIZE_DECIMALS)
    floored = (int(qty / step)) * step
    return f"{floored:.{SIZE_DECIMALS}f}"


# ─── Price tick (Bitget 45115: "price must be a multiple of <tick>") ─────────
# Bitget rejects any order price that isn't a multiple of the symbol's tick.
# tick = priceEndStep * 10^-pricePlace  (BTCUSDT -> 1 * 10^-1 = 0.1).
_PRICE_SPEC: dict = {}          # symbol -> (tick: float, place: int)
_BITGET_BASE = "https://api.bitget.com"


def _price_spec(symbol: str):
    if symbol in _PRICE_SPEC:
        return _PRICE_SPEC[symbol]
    spec = (0.1, 1)             # safe default = BTCUSDT, in case the fetch fails
    try:
        r = requests.get(
            f"{_BITGET_BASE}/api/v2/mix/market/contracts",
            params={"productType": BITGET_PRODUCT_TYPE, "symbol": symbol},
            timeout=8,
        )
        for c in (r.json() or {}).get("data") or []:
            if c.get("symbol") == symbol:
                place = int(c.get("pricePlace", 1))
                step  = int(c.get("priceEndStep", 1))
                spec  = (step / (10 ** place), place)
                break
    except Exception as e:
        log.warning(f"contract spec fetch failed for {symbol}, default {spec}: {e}")
    _PRICE_SPEC[symbol] = spec
    log.info(f"price tick for {symbol}: {spec[0]} ({spec[1]} dp)")
    return spec


def _round_price(symbol: str, price: float) -> str:
    """Snap a price to the symbol's tick so Bitget accepts it (fixes 45115)."""
    tick, place = _price_spec(symbol)
    rounded = round(round(price / tick) * tick, place)
    return f"{rounded:.{place}f}"


def _post(path: str, body: dict) -> dict:
    """Single choke point. Dry-run unless STAGE_LIVE; logs the exact payload."""
    if not is_live():
        log.warning(f"[DRY-RUN] would POST {path}  {body}")
        return {"dry_run": True, "path": path, "body": body,
                "data": {"orderId": f"dry-{int(time.time()*1000)}",
                         "clientOid": body.get("clientOid", "")}}
    res = _signed_request_elite("POST", path, params=None, body=body)
    return res.get("data") or {}


# ─── Order placement ─────────────────────────────────────────────────────────

def place_entry(symbol: str, side: str, entry: float, size: float,
                preset_sl: float, client_oid: str) -> dict:
    """
    Resting LIMIT entry at the level, with a PRESET stop attached so the fill is
    never naked even if the on-fill handler lags. side is LONG/SHORT.
    """
    body = {
        "symbol":      symbol,
        "productType": BITGET_PRODUCT_TYPE,
        "marginMode":  MARGIN_MODE,
        "marginCoin":  BITGET_MARGIN_COIN,
        "size":        _round_size(size),
        "price":       _round_price(symbol, entry),
        "side":        "buy" if side == "LONG" else "sell",
        "orderType":   "limit",
        "force":       "gtc",
        "reduceOnly":  "NO",
        "clientOid":   client_oid,
        "presetStopLossPrice": _round_price(symbol, preset_sl),
    }
    if not ONE_WAY_MODE:
        body["tradeSide"] = "open"
    return _post("/api/v2/mix/order/place-order", body)


def place_ladder_tps(symbol: str, side: str, tps: list, sizes: list,
                     client_oid_base: str) -> list:
    """
    One reduce-side profit_plan per TP rung. sizes aligned to tps (base coin).
    """
    hold = "long" if side == "LONG" else "short"
    out = []
    for i, (tp, sz) in enumerate(zip(tps, sizes), 1):
        if not tp or sz <= 0:
            continue
        body = {
            "marginCoin":  BITGET_MARGIN_COIN,
            "productType": BITGET_PRODUCT_TYPE,
            "symbol":      symbol,
            "planType":    "profit_plan",
            "triggerPrice": _round_price(symbol, tp),
            "triggerType": "mark_price",
            "executePrice": "0",          # 0 = market execution on trigger
            "holdSide":    hold,
            "size":        _round_size(sz),
            "clientOid":   f"{client_oid_base}-tp{i}",
        }
        out.append(_post("/api/v2/mix/order/place-tpsl-order", body))
    return out


def set_position_sl(symbol: str, side: str, sl_price: float) -> dict:
    """
    Ratchet primitive. Sets/updates the WHOLE-POSITION stop loss. Replaces any
    existing position SL (this is how BE / TP1 ratchet steps are applied).
    """
    hold = "long" if side == "LONG" else "short"
    body = {
        "marginCoin":  BITGET_MARGIN_COIN,
        "productType": BITGET_PRODUCT_TYPE,
        "symbol":      symbol,
        "stopLossTriggerPrice": _round_price(symbol, sl_price),
        "stopLossTriggerType":  "mark_price",
        "stopLossExecutePrice": "0",      # 0 = market on trigger
        "holdSide":    hold,
    }
    return _post("/api/v2/mix/order/place-pos-tpsl", body)


def cancel_plan_orders(symbol: str) -> dict:
    """Cancel resting plan/tpsl orders for a symbol. VERIFY param shape on your acct."""
    body = {
        "symbol":      symbol,
        "productType": BITGET_PRODUCT_TYPE,
        "marginCoin":  BITGET_MARGIN_COIN,
        "planType":    "profit_loss",
    }
    return _post("/api/v2/mix/order/cancel-plan-order", body)


def flatten(symbol: str) -> dict:
    """Market-close the position (kill switch). VERIFY endpoint on your acct."""
    body = {
        "symbol":      symbol,
        "productType": BITGET_PRODUCT_TYPE,
    }
    return _post("/api/v2/mix/order/close-positions", body)
