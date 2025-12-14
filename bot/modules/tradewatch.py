#!/usr/bin/env python3
"""
TradeWatch (Bitget) — Futures Executions + AI Checklist
======================================================

This module polls Bitget futures fills (MIX) and sends Telegram messages via send_func(text).
It also provides an AI checklist block (Structure/Liquidity/FVG) computed from 4H candles.

Key endpoints (Bitget V2 Futures):
- Fills (executions): GET /api/v2/mix/order/fills  (returns data.fillList)
- Candles: we try /api/v2/mix/market/candles then fallback to /api/v2/spot/market/candles

Environment variables:
- BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE (required)
- TRADEWATCH_ENABLED=1 (required to run watcher)
- TRADEWATCH_POLL_INTERVAL_SEC=10 (default 10)
- TRADEWATCH_PRODUCT_TYPE=USDT-FUTURES (default)  # USDT-FUTURES / COIN-FUTURES / USDC-FUTURES
- TRADEWATCH_SYMBOLS=BTCUSDT,ETHUSDT (default)    # comma-separated symbols; empty means ALL
- TRADEWATCH_CHECKLIST_ENABLED=1 (default 1)
- TRADEWATCH_DEGEN=1 (optional fun templates)

Notes:
- Fills use the futures "productType" parameter (required by Bitget).
- Bitget returns futures symbols lowercased in some fields; we normalize to upper.
"""

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
import base64
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

import requests

# =========================
# TradeWatch Configuration
# =========================

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET = os.environ.get("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "")
BITGET_BASE_URL = "https://api.bitget.com"

TRADEWATCH_ENABLED = os.environ.get("TRADEWATCH_ENABLED", "0") == "1"
TRADEWATCH_DEGEN = os.environ.get("TRADEWATCH_DEGEN", "0") == "1"
TRADEWATCH_POLL_INTERVAL_SEC = int(os.environ.get("TRADEWATCH_POLL_INTERVAL_SEC", "10"))

# Futures-specific
TRADEWATCH_PRODUCT_TYPE = os.environ.get("TRADEWATCH_PRODUCT_TYPE", "USDT-FUTURES")
# comma-separated list; default BTC+ETH; empty means no symbol filter
TRADEWATCH_SYMBOLS = [s.strip().upper() for s in os.environ.get("TRADEWATCH_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]

# Optional: enable checklist evaluation in alerts (default on)
TRADEWATCH_CHECKLIST_ENABLED = os.environ.get("TRADEWATCH_CHECKLIST_ENABLED", "1") == "1"

# =========================
# State (for /tradewatch_status)
# =========================

STATE: Dict[str, Any] = {
    "running": False,
    "last_poll_utc": None,
    "last_trade_utc": None,
    "last_trade_pair": None,
    "last_trade_side": None,
    "last_error": None,
    "last_checklist_utc": None,
    "last_checklist_symbol": None,
    "last_checklist_status": None,
}

# =========================
# Degen templates (optional)
# =========================

DEGEN_OPEN = [
    "🚀 Admin yeeted into a trade!",
    "💀 Admin just sent it.",
    "🧨 Admin deployed capital irresponsibly.",
    "🔥 Position opened — cope accordingly.",
    "🦍 Big ape energy detected.",
]

DEGEN_CLOSE = [
    "💼 Trade closed — consequences unknown.",
    "📉 Exit deployed (survived… barely).",
    "🪦 Position closed — funeral avoided.",
    "💸 Trade ended — PnL prayed for.",
]

# =========================
# Helpers
# =========================

def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _iso_or_none(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"

def _signed_request(method: str, request_path: str, params: dict | None = None, body: dict | None = None) -> dict:
    """
    Bitget V2 authenticated request.

    Signature format (Bitget V2):
      timestamp + method.toUpperCase() + requestPath + ("?" + query if query else "") + body
    """
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        raise RuntimeError("TradeWatch: Bitget API credentials missing")

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

    sig = hmac.new(
        BITGET_API_SECRET.encode("utf-8"),
        prehash.encode("utf-8"),
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
# Futures Fills (Executions)
# =========================

def _fetch_futures_fills(limit: int = 100, id_less_than: str | None = None) -> List[dict]:
    """
    Bitget Futures V2: Get Order Fill Details
      GET /api/v2/mix/order/fills?productType=USDT-FUTURES&symbol=BTCUSDT

    Returns: list of fill dicts from data.fillList
    Docs: https://www.bitget.com/api-doc/contract/trade/Get-Order-Fills
    """
    params: Dict[str, str] = {
        "productType": TRADEWATCH_PRODUCT_TYPE,
        "limit": str(min(max(limit, 1), 100)),
    }
    if id_less_than:
        params["idLessThan"] = str(id_less_than)

    # If symbols list is empty: no filter (Bitget allows symbol optional)
    # If symbols list has 1 item: request that symbol
    # If multiple: we fetch per symbol to avoid missing data
    # (Bitget endpoint supports symbol optional but can return all symbols; depends on account load)
    if len(TRADEWATCH_SYMBOLS) == 1:
        params["symbol"] = TRADEWATCH_SYMBOLS[0]

    data = _signed_request("GET", "/api/v2/mix/order/fills", params=params, body=None)
    fill_list = (data.get("data") or {}).get("fillList") or []
    return fill_list

def _fetch_futures_fills_multi(limit_each: int = 60) -> List[dict]:
    """
    If tracking multiple symbols, query each symbol and merge results.
    """
    if not TRADEWATCH_SYMBOLS:
        return _fetch_futures_fills(limit=100)

    out: List[dict] = []
    for sym in TRADEWATCH_SYMBOLS:
        params: Dict[str, str] = {
            "productType": TRADEWATCH_PRODUCT_TYPE,
            "symbol": sym,
            "limit": str(min(max(limit_each, 1), 100)),
        }
        data = _signed_request("GET", "/api/v2/mix/order/fills", params=params, body=None)
        out.extend(((data.get("data") or {}).get("fillList") or []))
    return out

def _classify_execution(fill: dict) -> str:
    """
    Futures fill fields (per docs):
      tradeSide: open / close / reduce_close_long / ...
      tradeScope: maker / taker
      side: buy / sell
    """
    trade_side = (fill.get("tradeSide") or "").lower()
    if trade_side == "open" or "open" in trade_side:
        return "Position Open/Increase"
    if trade_side == "close" or "close" in trade_side:
        return "Position Close/Reduce"
    return "Execution"

def _format_message(fill: dict, checklist_block: str | None = None) -> str:
    """
    Telegram message format for futures fills.
    """
    pair = (fill.get("symbol") or "N/A").upper()
    side_raw = (fill.get("side") or "N/A").upper()
    price = fill.get("price") or fill.get("priceAvg") or "N/A"
    # futures: baseVolume is size in base coin units
    size = fill.get("baseVolume") or fill.get("size") or fill.get("amount") or "N/A"
    trade_id = fill.get("tradeId") or fill.get("id") or ""
    trade_scope = (fill.get("tradeScope") or "").lower()
    trade_side = (fill.get("tradeSide") or "").lower()

    if TRADEWATCH_DEGEN:
        header = random.choice(DEGEN_OPEN if "open" in trade_side else DEGEN_CLOSE)
    else:
        header = "📈 [TradeWatch] Futures Execution"

    if side_raw == "BUY":
        side_emoji = "🟢"
    elif side_raw == "SELL":
        side_emoji = "🔴"
    else:
        side_emoji = "📘"

    execution = _classify_execution(fill)
    maker_taker = f"{trade_scope}".upper() if trade_scope else "—"

    msg = (
        f"{side_emoji} {header}\n"
        f"Pair: {pair}\n"
        f"Side: {side_raw}\n"
        f"Price: {price}\n"
        f"Size: {size}\n"
        f"Execution: {execution}\n"
        f"Fill: {maker_taker} | tradeSide: {trade_side or '—'}\n"
        f"TradeId: {trade_id}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )

    if checklist_block:
        msg += "\n\n" + checklist_block

    return msg

def get_status() -> str:
    symbols = ",".join(TRADEWATCH_SYMBOLS) if TRADEWATCH_SYMBOLS else "ALL"
    lines = [
        "📈 [TradeWatch] Status",
        f"Enabled: {'Yes ✅' if TRADEWATCH_ENABLED else 'No ❌'}",
        f"ProductType: {TRADEWATCH_PRODUCT_TYPE}",
        f"Symbols: {symbols}",
        f"Running loop: {'Yes ✅' if STATE['running'] else 'No ❌'}",
        f"Last poll (UTC): {_iso_or_none(STATE['last_poll_utc'])}",
        f"Last trade (UTC): {_iso_or_none(STATE['last_trade_utc'])}",
    ]
    if STATE.get("last_trade_pair"):
        lines.append(f"Last trade: {STATE['last_trade_pair']} {STATE.get('last_trade_side','')}".strip())
    if STATE.get("last_checklist_status"):
        lines.append(
            f"Last checklist: {STATE.get('last_checklist_symbol','?')} — {STATE['last_checklist_status']} ({_iso_or_none(STATE['last_checklist_utc'])})"
        )
    if STATE.get("last_error"):
        lines.append(f"Last error: {STATE['last_error']}")
    return "\n".join(lines)

def start_tradewatch(send_func):
    """
    Main polling loop. Provide a send_func(text:str) that sends to Telegram.
    """
    if not TRADEWATCH_ENABLED:
        print("[TradeWatch] Disabled (TRADEWATCH_ENABLED != 1)")
        STATE["running"] = False
        return

    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSPHRASE):
        print("[TradeWatch] Missing Bitget API credentials.")
        STATE["running"] = False
        return

    print("[TradeWatch] Watcher started (FUTURES fills)...")
    STATE["running"] = True

    seen: set[str] = set()
    first_run = True

    while True:
        try:
            STATE["last_poll_utc"] = datetime.now(timezone.utc)

            fills = _fetch_futures_fills_multi(limit_each=60)

            if first_run:
                for f in fills:
                    tid = f.get("tradeId") or f.get("id")
                    if tid:
                        seen.add(str(tid))
                first_run = False
            else:
                # Send oldest unseen first (sort by cTime)
                def _ctime(x: dict) -> int:
                    try:
                        return int(x.get("cTime") or 0)
                    except Exception:
                        return 0

                fills_sorted = sorted(fills, key=_ctime)
                for f in fills_sorted:
                    tid = f.get("tradeId") or f.get("id")
                    if not tid:
                        continue
                    tid = str(tid)
                    if tid in seen:
                        continue
                    seen.add(tid)

                    # Normalize symbol filter if TRADEWATCH_SYMBOLS set
                    sym = (f.get("symbol") or "").upper()
                    if TRADEWATCH_SYMBOLS and sym and sym not in TRADEWATCH_SYMBOLS:
                        continue

                    STATE["last_trade_utc"] = datetime.now(timezone.utc)
                    STATE["last_trade_pair"] = sym or f.get("symbol")
                    STATE["last_trade_side"] = (f.get("side") or "").upper()
                    STATE["last_error"] = None

                    checklist_block = None
                    if TRADEWATCH_CHECKLIST_ENABLED and sym:
                        try:
                            checklist_block = get_checklist_status_text(sym, include_reasons=False)
                        except Exception as ce:
                            checklist_block = f"🧠 Checklist: unavailable ({ce})"

                    send_func(_format_message(f, checklist_block=checklist_block))

                if len(seen) > 4000:
                    seen = set(list(seen)[-2500:])

        except Exception as e:
            STATE["last_error"] = str(e)
            print("[TradeWatch] Error:", e)

        time.sleep(TRADEWATCH_POLL_INTERVAL_SEC)

# ============================================================
# AI Checklist (Structure / Liquidity / FVG) — 4H candles
# ============================================================

Candle = Dict[str, float]

@dataclass
class ChecklistResult:
    status: str
    bias: str
    score: int
    max_score: int
    structure: "CheckResult"
    liquidity: "CheckResult"
    fvg: "CheckResult"

@dataclass
class CheckResult:
    ok: bool
    score: int
    max_score: int
    bias: str
    reasons: List[str]
    details: Dict[str, object]

def bitget_klines_to_candles(raw: Any) -> List[Candle]:
    data = raw.get("data") if isinstance(raw, dict) else raw
    if not data:
        return []
    out: List[Candle] = []
    for row in data:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            ts = float(row[0])
            if ts > 1e12:
                ts /= 1000.0
            out.append({
                "ts": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        elif isinstance(row, dict):
            ts = row.get("ts") or row.get("candleTime") or row.get("timestamp") or row.get("time")
            o = row.get("open") or row.get("o")
            h = row.get("high") or row.get("h")
            l = row.get("low") or row.get("l")
            c = row.get("close") or row.get("c")
            v = row.get("volume") or row.get("v") or row.get("baseVol") or row.get("quoteVol") or 0.0
            if ts is None or o is None or h is None or l is None or c is None:
                continue
            ts = float(ts)
            if ts > 1e12:
                ts /= 1000.0
            out.append({"ts": ts, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)})
    out.sort(key=lambda x: x["ts"])
    return out

def fetch_candles_4h(symbol: str, limit: int = 320) -> List[Candle]:
    # MIX candles
    try:
        raw = _signed_request(
            "GET",
            "/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": "4H", "limit": str(limit), "productType": TRADEWATCH_PRODUCT_TYPE},
            body=None,
        )
        candles = bitget_klines_to_candles(raw)
        if candles:
            return candles
    except Exception:
        pass

    # SPOT fallback (should still work for BTCUSDT/ETHUSDT if needed)
    raw = _signed_request(
        "GET",
        "/api/v2/spot/market/candles",
        params={"symbol": symbol, "granularity": "4H", "limit": str(limit)},
        body=None,
    )
    return bitget_klines_to_candles(raw)

def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def _atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0

def _pivot_high(candles: List[Candle], i: int, left: int = 2, right: int = 2) -> bool:
    if i - left < 0 or i + right >= len(candles):
        return False
    h = candles[i]["high"]
    for j in range(i - left, i + right + 1):
        if j != i and candles[j]["high"] >= h:
            return False
    return True

def _pivot_low(candles: List[Candle], i: int, left: int = 2, right: int = 2) -> bool:
    if i - left < 0 or i + right >= len(candles):
        return False
    l = candles[i]["low"]
    for j in range(i - left, i + right + 1):
        if j != i and candles[j]["low"] <= l:
            return False
    return True

def _last_n_pivots(candles: List[Candle], kind: str, n: int = 2, left: int = 2, right: int = 2) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    for i in range(len(candles) - right - 1, left, -1):
        if kind == "high" and _pivot_high(candles, i, left, right):
            out.append((i, candles[i]["high"]))
        elif kind == "low" and _pivot_low(candles, i, left, right):
            out.append((i, candles[i]["low"]))
        if len(out) >= n:
            break
    return list(reversed(out))

def check_structure(candles: List[Candle], ema_period: int = 200) -> CheckResult:
    if len(candles) < max(ema_period + 20, 120):
        return CheckResult(False, 0, 4, "NEUTRAL", ["Not enough candles for structure/EMA."], {})

    highs = _last_n_pivots(candles, "high", n=2)
    lows = _last_n_pivots(candles, "low", n=2)

    if len(highs) < 2 or len(lows) < 2:
        return CheckResult(False, 0, 4, "NEUTRAL", ["Not enough pivot points."], {"highs": highs, "lows": lows})

    (_, h1), (_, h2) = highs
    (_, l1), (_, l2) = lows

    bullish = (h2 > h1) and (l2 > l1)
    bearish = (h2 < h1) and (l2 < l1)

    closes = [c["close"] for c in candles]
    ema = _ema(closes, ema_period)
    last_close = closes[-1]
    last_ema = ema[-1]

    score = 0
    reasons: List[str] = []
    bias = "NEUTRAL"

    if bullish:
        bias = "LONG"
        score += 2
        reasons.append(f"Structure: HH/HL (H {h1:.0f}->{h2:.0f}, L {l1:.0f}->{l2:.0f}).")
    elif bearish:
        bias = "SHORT"
        score += 2
        reasons.append(f"Structure: LH/LL (H {h1:.0f}->{h2:.0f}, L {l1:.0f}->{l2:.0f}).")
    else:
        reasons.append("Structure: mixed pivots (range/transition).")

    if bias == "LONG" and last_close > last_ema:
        score += 2
        reasons.append(f"EMA{ema_period}: close above.")
    elif bias == "SHORT" and last_close < last_ema:
        score += 2
        reasons.append(f"EMA{ema_period}: close below.")
    elif bias != "NEUTRAL":
        reasons.append(f"EMA{ema_period}: not aligned (close {last_close:.0f} vs {last_ema:.0f}).")

    ok = bias in ("LONG", "SHORT") and score >= 2
    return CheckResult(ok, score, 4, bias, reasons, {"close": last_close, "ema": last_ema})

def check_liquidity(candles: List[Candle], lookback: int = 24, reclaim_required: bool = True) -> CheckResult:
    if len(candles) < lookback + 5:
        return CheckResult(False, 0, 3, "NEUTRAL", ["Not enough candles for liquidity check."], {})

    atr = _atr(candles, 14)
    last = candles[-1]
    lb = candles[-lookback:]

    recent_high = max(c["high"] for c in lb[:-1])
    recent_low = min(c["low"] for c in lb[:-1])

    margin = atr * 0.15 if atr > 0 else (recent_high - recent_low) * 0.01

    swept_high = last["high"] > (recent_high + margin)
    swept_low = last["low"] < (recent_low - margin)

    reclaimed_after_low = last["close"] > recent_low
    reclaimed_after_high = last["close"] < recent_high

    score = 0
    reasons: List[str] = []
    bias = "NEUTRAL"

    if swept_low:
        bias = "LONG"
        score += 1
        reasons.append(f"Sweep: sell-side below {recent_low:.0f}.")
        if reclaim_required and reclaimed_after_low:
            score += 2
            reasons.append("Reclaim: close back above swept low.")
        elif reclaim_required:
            reasons.append("Reclaim: not yet (wait).")
        else:
            score += 1
    elif swept_high:
        bias = "SHORT"
        score += 1
        reasons.append(f"Sweep: buy-side above {recent_high:.0f}.")
        if reclaim_required and reclaimed_after_high:
            score += 2
            reasons.append("Reclaim: close back below swept high.")
        elif reclaim_required:
            reasons.append("Reclaim: not yet (wait).")
        else:
            score += 1
    else:
        reasons.append("No clear sweep detected.")

    ok = score >= (3 if reclaim_required else 2)
    return CheckResult(ok, score, 3, bias, reasons, {"recent_high": recent_high, "recent_low": recent_low, "atr": atr, "margin": margin})

def check_fvg(candles: List[Candle], max_lookback: int = 80) -> CheckResult:
    if len(candles) < 10:
        return CheckResult(False, 0, 3, "NEUTRAL", ["Not enough candles for FVG check."], {})

    atr = _atr(candles, 14)
    min_gap = atr * 0.05 if atr > 0 else 0.0

    zones: List[Tuple[str, float, float, int]] = []
    start = max(2, len(candles) - max_lookback)
    for i in range(start, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]
        if c1["high"] + min_gap < c3["low"]:
            zones.append(("bullish", c1["high"], c3["low"], i))
        if c1["low"] - min_gap > c3["high"]:
            zones.append(("bearish", c3["high"], c1["low"], i))

    if not zones:
        return CheckResult(False, 0, 3, "NEUTRAL", ["No recent FVG found."], {})

    kind, z_low, z_high, _ = zones[-1]
    last = candles[-1]
    close = last["close"]
    mid = (z_low + z_high) / 2

    touched = (last["low"] <= z_high and last["high"] >= z_low)
    if not touched:
        return CheckResult(False, 0, 3, "NEUTRAL", ["No active FVG interaction."], {"zone": (kind, z_low, z_high)})

    score = 1
    reasons: List[str] = [f"FVG touched: {kind} [{z_low:.0f}-{z_high:.0f}]."]
    bias = "LONG" if kind == "bullish" else "SHORT"

    rng = max(1e-9, last["high"] - last["low"])
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    wick_ok = (lower_ratio >= 0.45) if kind == "bullish" else (upper_ratio >= 0.45)
    close_ok = (close >= mid) if kind == "bullish" else (close <= mid)

    if wick_ok:
        score += 1
        reasons.append("Reaction wick confirmed.")
    else:
        reasons.append("Weak wick reaction.")

    if close_ok:
        score += 1
        reasons.append("Close confirms direction vs midpoint.")
    else:
        reasons.append("Close not confirming (lower confidence).")

    return CheckResult(score >= 2, score, 3, bias, reasons, {"zone": (kind, z_low, z_high), "mid": mid})

def evaluate_checklist(symbol: str) -> ChecklistResult:
    candles = fetch_candles_4h(symbol)
    if not candles:
        s = CheckResult(False, 0, 4, "NEUTRAL", ["No candles returned from Bitget."], {})
        l = CheckResult(False, 0, 3, "NEUTRAL", ["No candles returned from Bitget."], {})
        f = CheckResult(False, 0, 3, "NEUTRAL", ["No candles returned from Bitget."], {})
        return ChecklistResult("🔴 NO DATA", "NEUTRAL", 0, 10, s, l, f)

    s = check_structure(candles)
    l = check_liquidity(candles)
    f = check_fvg(candles)

    structure_ok = s.ok and s.bias in ("LONG", "SHORT")
    setup_ok = ((l.ok and l.bias == s.bias) or (f.ok and f.bias == s.bias))

    score = s.score + l.score + f.score
    max_score = s.max_score + l.max_score + f.max_score

    if structure_ok and setup_ok:
        status = "✅ SETUP VALID"
        bias = s.bias
    elif structure_ok:
        status = "🟡 PARTIAL (WAIT)"
        bias = s.bias
    else:
        status = "🔴 NO TRADE"
        bias = "NEUTRAL"

    STATE["last_checklist_utc"] = datetime.now(timezone.utc)
    STATE["last_checklist_symbol"] = symbol
    STATE["last_checklist_status"] = status

    return ChecklistResult(status, bias, score, max_score, s, l, f)

def get_checklist_status_text(symbol: str, include_reasons: bool = True) -> str:
    res = evaluate_checklist(symbol)
    lines = [
        "🧠 [AI Checklist]",
        f"Symbol: {symbol}",
        f"Status: {res.status}",
        f"Bias: {res.bias}",
        f"Score: {res.score}/{res.max_score}",
    ]
    if include_reasons:
        lines.append("")
        lines.append("Structure:")
        for r in res.structure.reasons[:5]:
            lines.append(f"• {r}")
        lines.append("Liquidity:")
        for r in res.liquidity.reasons[:5]:
            lines.append(f"• {r}")
        lines.append("FVG:")
        for r in res.fvg.reasons[:5]:
            lines.append(f"• {r}")
    return "\n".join(lines)