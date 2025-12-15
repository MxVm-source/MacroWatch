#!/usr/bin/env python3
"""
TradeWatch (Bitget) — Futures Executions + AI Checklist + Auto Setup Alerts + TP Hit Updates
==========================================================================================
- Watches Bitget Futures (MIX) fills/executions and sends Telegram messages via send_func(text)
- Computes an AI checklist (Structure / Liquidity / FVG) from 4H candles
- Optionally sends Auto AI Setup Alerts even when you haven't executed a trade yet
- ✅ NEW: builds a clean plan (Entry/SL/TP1/TP2/TP3) and sends it with the AI alert
- ✅ NEW: TP hit watcher sends TP1 / TP2 / TP3 hit updates automatically

Env vars (new)
--------------
TP tracking:
- TRADEWATCH_TP_ALERTS=1            # enable TP hit watcher
- TRADEWATCH_TP_POLL_SEC=15         # price polling interval
- TRADEWATCH_TP_REQUIRE_PLAN=1      # if 1, only track TPs after a plan exists (AI alert generated)
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
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple, Optional, Callable

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

def _parse_symbols(raw: str) -> List[str]:
    out: List[str] = []
    for s in (raw or "").split(","):
        s = (s or "").strip().upper()
        if not s:
            continue
        if s.endswith(".P"):
            s = s[:-2]
        out.append(s)
    return out

TRADEWATCH_SYMBOLS = _parse_symbols(os.environ.get("TRADEWATCH_SYMBOLS", "BTCUSDT,ETHUSDT"))

# Checklist
TRADEWATCH_CHECKLIST_ENABLED = os.environ.get("TRADEWATCH_CHECKLIST_ENABLED", "1") == "1"
TRADEWATCH_CHECKLIST_GRANULARITY = os.environ.get("TRADEWATCH_CHECKLIST_GRANULARITY", "4H")  # "4H" or "240"

# Auto AI setup alerts (no trade needed)
TRADEWATCH_AI_ALERTS = os.environ.get("TRADEWATCH_AI_ALERTS", "0") == "1"
TRADEWATCH_AI_MIN_SCORE = int(os.environ.get("TRADEWATCH_AI_MIN_SCORE", "6"))
TRADEWATCH_AI_INTERVAL_SEC = int(os.environ.get("TRADEWATCH_AI_INTERVAL_SEC", "60"))
TRADEWATCH_AI_COOLDOWN_MIN = int(os.environ.get("TRADEWATCH_AI_COOLDOWN_MIN", "180"))
TRADEWATCH_AI_SEND_PARTIAL = os.environ.get("TRADEWATCH_AI_SEND_PARTIAL", "0") == "1"

# ✅ TP hit updates
TRADEWATCH_TP_ALERTS = os.environ.get("TRADEWATCH_TP_ALERTS", "0") == "1"
TRADEWATCH_TP_POLL_SEC = int(os.environ.get("TRADEWATCH_TP_POLL_SEC", "15"))
TRADEWATCH_TP_REQUIRE_PLAN = os.environ.get("TRADEWATCH_TP_REQUIRE_PLAN", "1") == "1"

# =========================
# State
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
    "last_ai_scan_utc": None,
    "last_tp_scan_utc": None,
}

# Per-symbol state for AI alerts + /setup_status
SETUP_STATE: Dict[str, Dict[str, Any]] = {}

# ✅ Plan + TP progress (per symbol)
# PLAN_STATE[sym] = {"bias","entry_zone","sl","tps":[...], "created_utc", "tp_hits":[bool,bool,bool], "last_price": float|None}
PLAN_STATE: Dict[str, Dict[str, Any]] = {}

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

def _normalize_symbol(sym: str) -> str:
    sym = (sym or "").strip().upper()
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym

def _signed_request(method: str, request_path: str, params: dict | None = None, body: dict | None = None) -> dict:
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

def _public_get(request_path: str, params: dict) -> dict:
    from urllib.parse import urlencode
    url = f"{BITGET_BASE_URL}{request_path}?{urlencode(params)}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Bitget HTTP {r.status_code}: {r.text}")
    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget API error {data.get('code')}: {data.get('msg')}")
    return data

# =========================
# Public price (ticker)
# =========================

def fetch_last_price(symbol: str) -> Optional[float]:
    """
    Tries MIX ticker first, then spot.
    Returns last price as float or None.
    """
    symbol = _normalize_symbol(symbol)
    # MIX ticker
    try:
        raw = _public_get(
            "/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": TRADEWATCH_PRODUCT_TYPE},
        )
        d = (raw.get("data") or {})
        # Bitget often returns {"lastPr":"..."} or {"last":"..."} depending on market
        for k in ("lastPr", "last", "close", "lastPrice"):
            v = d.get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass

    # SPOT ticker fallback
    try:
        raw = _public_get("/api/v2/spot/market/ticker", params={"symbol": symbol})
        d = (raw.get("data") or {})
        for k in ("lastPr", "last", "close", "lastPrice"):
            v = d.get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass

    return None

# =========================
# Futures Fills (Executions)
# =========================

def _fetch_futures_fills(limit: int = 100, symbol: str | None = None) -> List[dict]:
    params: Dict[str, str] = {
        "productType": TRADEWATCH_PRODUCT_TYPE,
        "limit": str(min(max(limit, 1), 100)),
    }
    if symbol:
        params["symbol"] = _normalize_symbol(symbol)

    data = _signed_request("GET", "/api/v2/mix/order/fills", params=params, body=None)
    return ((data.get("data") or {}).get("fillList") or [])

def _fetch_futures_fills_multi(limit_each: int = 60) -> List[dict]:
    if not TRADEWATCH_SYMBOLS:
        return _fetch_futures_fills(limit=100, symbol=None)

    out: List[dict] = []
    for sym in TRADEWATCH_SYMBOLS:
        out.extend(_fetch_futures_fills(limit=limit_each, symbol=sym))
    return out

def _classify_execution(fill: dict) -> str:
    trade_side = (fill.get("tradeSide") or "").lower()
    scope = (fill.get("orderType") or fill.get("tradeScope") or "").lower()

    if "take" in scope or "tp" in scope:
        return "Take Profit"
    if "stop" in scope or "sl" in scope:
        return "Stop Loss"
    if "open" in trade_side:
        return "Position Open/Increase"
    if "close" in trade_side or "reduce" in trade_side:
        return "Position Close/Reduce"
    return "Execution"

def _format_message(fill: dict, checklist_block: str | None = None) -> str:
    pair = _normalize_symbol(fill.get("symbol") or "N/A")
    side_raw = (fill.get("side") or "N/A").upper()
    price = fill.get("price") or fill.get("priceAvg") or "N/A"
    size = fill.get("baseVolume") or fill.get("size") or fill.get("amount") or "N/A"
    trade_id = fill.get("tradeId") or fill.get("id") or ""
    trade_scope = (fill.get("tradeScope") or "").lower()
    trade_side = (fill.get("tradeSide") or "").lower()

    if TRADEWATCH_DEGEN:
        header = random.choice(DEGEN_OPEN if "open" in trade_side else DEGEN_CLOSE)
    else:
        header = "📈 [TradeWatch] Futures Execution"

    side_emoji = "🟢" if side_raw == "BUY" else ("🔴" if side_raw == "SELL" else "📘")
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
        f"Polling: {TRADEWATCH_POLL_INTERVAL_SEC}s",
        f"AI alerts: {'Yes ✅' if TRADEWATCH_AI_ALERTS else 'No ❌'} (every {TRADEWATCH_AI_INTERVAL_SEC}s, min {TRADEWATCH_AI_MIN_SCORE}/10)",
        f"TP alerts: {'Yes ✅' if TRADEWATCH_TP_ALERTS else 'No ❌'} (every {TRADEWATCH_TP_POLL_SEC}s)",
        f"Running loop: {'Yes ✅' if STATE['running'] else 'No ❌'}",
        f"Last poll (UTC): {_iso_or_none(STATE['last_poll_utc'])}",
        f"Last trade (UTC): {_iso_or_none(STATE['last_trade_utc'])}",
        f"Last AI scan (UTC): {_iso_or_none(STATE.get('last_ai_scan_utc'))}",
        f"Last TP scan (UTC): {_iso_or_none(STATE.get('last_tp_scan_utc'))}",
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
    out.sort(key=lambda x: x["ts"])
    return out

def fetch_candles(symbol: str, granularity: str, limit: int = 320) -> List[Candle]:
    symbol = _normalize_symbol(symbol)
    try:
        raw = _public_get(
            "/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": granularity, "limit": str(limit), "productType": TRADEWATCH_PRODUCT_TYPE},
        )
        candles = bitget_klines_to_candles(raw)
        if candles:
            return candles
    except Exception:
        pass

    raw = _public_get(
        "/api/v2/spot/market/candles",
        params={"symbol": symbol, "granularity": granularity, "limit": str(limit)},
    )
    return bitget_klines_to_candles(raw)

def fetch_candles_4h(symbol: str, limit: int = 320) -> List[Candle]:
    for g in [TRADEWATCH_CHECKLIST_GRANULARITY, "4H", "240"]:
        g = str(g).strip()
        if not g:
            continue
        try:
            c = fetch_candles(symbol, g, limit=limit)
            if c:
                return c
        except Exception:
            continue
    return []

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
    symbol = _normalize_symbol(symbol)
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
    symbol = _normalize_symbol(symbol)
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

# =========================
# Plan builder (Entry/SL/TPs) + TP tracking
# =========================

def _compute_levels_from_candles(candles: List[Candle], lookback: int = 48) -> Optional[Dict[str, float]]:
    if not candles:
        return None
    lb = candles[-lookback:] if len(candles) > lookback else candles
    support = min(c["low"] for c in lb)
    resistance = max(c["high"] for c in lb)
    last = candles[-1]["close"]
    return {"support": support, "resistance": resistance, "last": last}

def _atr_simple(candles: List[Candle], period: int = 14) -> float:
    return _atr(candles, period)

def build_plan(symbol: str) -> Dict[str, Any]:
    """
    Builds a simple plan aligned with checklist bias using 4H S/R + ATR buffer.
    Returns dict with entry_zone, sl, tps, etc.
    """
    sym = _normalize_symbol(symbol)
    candles = fetch_candles_4h(sym, limit=220)
    if not candles:
        return {"symbol": sym, "error": "No candles returned."}

    levels = _compute_levels_from_candles(candles, lookback=48)
    if not levels:
        return {"symbol": sym, "error": "Not enough candle data."}

    atrv = _atr_simple(candles, 14)
    chk = evaluate_checklist(sym)

    last = float(levels["last"])
    sup = float(levels["support"])
    res = float(levels["resistance"])

    bias = chk.bias
    atr_buf = max(atrv * 0.35, last * 0.0015)

    if bias == "LONG":
        entry_lo = sup + atr_buf * 0.2
        entry_hi = sup + atr_buf * 1.2
        sl = sup - atr_buf * 1.2
        tp1 = last + (res - last) * 0.35
        tp2 = last + (res - last) * 0.70
        tp3 = res
    elif bias == "SHORT":
        entry_lo = res - atr_buf * 1.2
        entry_hi = res - atr_buf * 0.2
        sl = res + atr_buf * 1.2
        tp1 = last - (last - sup) * 0.35
        tp2 = last - (last - sup) * 0.70
        tp3 = sup
    else:
        # neutral / range fallback
        entry_lo = sup + atr_buf * 0.2
        entry_hi = sup + atr_buf * 1.0
        sl = sup - atr_buf * 1.2
        tp1 = last
        tp2 = last + (res - last) * 0.50
        tp3 = res

    plan = {
        "symbol": sym,
        "status": chk.status,
        "bias": bias,
        "score": f"{chk.score}/{chk.max_score}",
        "last": last,
        "support": sup,
        "resistance": res,
        "atr": atrv,
        "entry_zone": (float(entry_lo), float(entry_hi)),
        "sl": float(sl),
        "tps": [float(tp1), float(tp2), float(tp3)],
    }

    # store plan for TP watcher
    PLAN_STATE[sym] = {
        "bias": bias,
        "entry_zone": plan["entry_zone"],
        "sl": plan["sl"],
        "tps": plan["tps"],
        "created_utc": datetime.now(timezone.utc),
        "tp_hits": [False, False, False],
        "last_price": None,
    }
    return plan

def _tp_hit(bias: str, price: float, target: float) -> bool:
    b = (bias or "").upper()
    if b == "LONG":
        return price >= target
    if b == "SHORT":
        return price <= target
    return False

def get_tp_status_text() -> str:
    symbols = TRADEWATCH_SYMBOLS or ["BTCUSDT", "ETHUSDT"]
    lines = ["🎯 [TP Status]"]

    for s in symbols:
        sym = _normalize_symbol(s)
        st = PLAN_STATE.get(sym)
        if not st:
            lines.append(f"{sym} — no active plan yet.")
            continue

        bias = st.get("bias", "—")
        tps = st.get("tps") or []
        hits = st.get("tp_hits") or [False, False, False]
        last_price = st.get("last_price")
        last_str = f"{last_price:.2f}" if isinstance(last_price, (int, float)) else "—"

        def _mark(i: int) -> str:
            return "✅" if hits[i] else "⏳"

        if len(tps) >= 3:
            lines.append(
                f"{sym} — {bias} | last: {last_str} | "
                f"TP1 {_mark(0)} {tps[0]:.0f} | TP2 {_mark(1)} {tps[1]:.0f} | TP3 {_mark(2)} {tps[2]:.0f}"
            )
        else:
            lines.append(f"{sym} — {bias} | last: {last_str} | no TP levels stored.")

    return "\n".join(lines)

def _format_plan_block(plan: Dict[str, Any]) -> str:
    if plan.get("error"):
        return f"⚠️ Plan unavailable: {plan['error']}"
    ez = plan["entry_zone"]
    tps = plan["tps"]
    return (
        "🧾 Plan (auto)\n"
        f"• Entry: {ez[0]:.0f} – {ez[1]:.0f}\n"
        f"• SL: {plan['sl']:.0f}\n"
        f"• TP1: {tps[0]:.0f}\n"
        f"• TP2: {tps[1]:.0f}\n"
        f"• TP3: {tps[2]:.0f}"
    )

def start_tp_hit_watcher(send_func: Callable[[str], None]) -> None:
    if not TRADEWATCH_TP_ALERTS:
        print("[TradeWatch] TP hit watcher disabled (TRADEWATCH_TP_ALERTS != 1)")
        return

    print("[TradeWatch] TP hit watcher started ✅")

    symbols = TRADEWATCH_SYMBOLS or ["BTCUSDT", "ETHUSDT"]

    while True:
        try:
            STATE["last_tp_scan_utc"] = datetime.now(timezone.utc)

            for s in symbols:
                sym = _normalize_symbol(s)

                if TRADEWATCH_TP_REQUIRE_PLAN and sym not in PLAN_STATE:
                    continue

                st = PLAN_STATE.get(sym)
                if not st:
                    continue

                price = fetch_last_price(sym)
                if price is None:
                    continue

                st["last_price"] = price

                bias = (st.get("bias") or "NEUTRAL").upper()
                tps = st.get("tps") or []
                hits = st.get("tp_hits") or [False, False, False]

                if len(tps) < 3:
                    continue

                # sequential updates (TP1 then TP2 then TP3)
                for i in range(3):
                    if hits[i]:
                        continue
                    if _tp_hit(bias, price, float(tps[i])):
                        hits[i] = True
                        st["tp_hits"] = hits

                        pct = (i + 1) * 33
                        send_func(
                            "🎯 [TP HIT]\n"
                            f"Pair: {sym}\n"
                            f"Bias: {bias}\n"
                            f"TP{i+1} hit: {tps[i]:.0f}\n"
                            f"Price: {price:.2f}\n"
                            f"Progress: {pct}%\n"
                            f"Time (UTC): {_iso_utc_now()}"
                        )
                        # only fire one TP per cycle to avoid spam if it gaps through multiple
                        break

        except Exception as e:
            STATE["last_error"] = f"TP watcher error: {e}"
            print("[TradeWatch] TP watcher error:", e)

        time.sleep(TRADEWATCH_TP_POLL_SEC)

# =========================
# Auto AI Setup Alerts + /setup_status
# =========================

def _bias_emoji(bias: str) -> str:
    b = (bias or "").upper()
    if b == "LONG":
        return "🟢"
    if b == "SHORT":
        return "🔴"
    return "⚪️"

def _status_emoji(status: str) -> str:
    if "SETUP VALID" in status:
        return "✅"
    if "PARTIAL" in status:
        return "🟡"
    if "NO TRADE" in status or "NO DATA" in status:
        return "🔴"
    return "⚪️"

def _should_alert(sym: str, res: ChecklistResult) -> bool:
    sym = _normalize_symbol(sym)
    is_valid = "SETUP VALID" in res.status
    is_partial = "PARTIAL" in res.status

    if is_valid:
        if res.score < TRADEWATCH_AI_MIN_SCORE:
            return False
    elif is_partial:
        if not TRADEWATCH_AI_SEND_PARTIAL:
            return False
    else:
        return False

    now = datetime.now(timezone.utc)

    if is_valid:
        last_alert = SETUP_STATE.get(sym, {}).get("last_alert_utc")
        if last_alert and (now - last_alert) < timedelta(minutes=TRADEWATCH_AI_COOLDOWN_MIN):
            return False

    prev = SETUP_STATE.get(sym, {})
    if prev.get("last_status") == res.status and prev.get("last_bias") == res.bias:
        return False

    prev_status = prev.get("last_status", "—")
    if is_valid and ("SETUP VALID" not in prev_status):
        return True
    if is_partial and ("PARTIAL" not in prev_status):
        return True

    return False

def _format_ai_alert(sym: str, res: ChecklistResult) -> str:
    sym = _normalize_symbol(sym)
    se = _status_emoji(res.status)
    be = _bias_emoji(res.bias)

    # ✅ build & attach plan so group gets entry/sl/tps immediately
    plan = build_plan(sym)
    plan_block = _format_plan_block(plan)

    return (
        f"{se} [AI SETUP ALERT]\n"
        f"Pair: {sym}\n"
        f"Bias: {be} {res.bias}\n"
        f"Score: {res.score}/{res.max_score}\n"
        f"Status: {res.status}\n\n"
        f"{plan_block}\n\n"
        f"Fast checks:\n"
        f"• Structure: {res.structure.bias} ({res.structure.score}/{res.structure.max_score})\n"
        f"• Liquidity: {res.liquidity.bias} ({res.liquidity.score}/{res.liquidity.max_score})\n"
        f"• FVG: {res.fvg.bias} ({res.fvg.score}/{res.fvg.max_score})\n"
        f"Time (UTC): {_iso_utc_now()}"
    )

def start_ai_setup_alerts(send_func: Callable[[str], None]) -> None:
    if not TRADEWATCH_AI_ALERTS:
        print("[TradeWatch] AI setup alerts disabled (TRADEWATCH_AI_ALERTS != 1)")
        return

    symbols = TRADEWATCH_SYMBOLS or ["BTCUSDT", "ETHUSDT"]

    for s in symbols:
        sym = _normalize_symbol(s)
        SETUP_STATE.setdefault(sym, {"last_status": "—", "last_bias": "—", "last_score": "—", "last_alert_utc": None})
        try:
            r0 = evaluate_checklist(sym)
            SETUP_STATE[sym]["last_status"] = r0.status
            SETUP_STATE[sym]["last_bias"] = r0.bias
            SETUP_STATE[sym]["last_score"] = f"{r0.score}/{r0.max_score}"
        except Exception as e:
            STATE["last_error"] = f"AI init error {sym}: {e}"

    print("[TradeWatch] AI setup alerts started ✅")

    while True:
        try:
            STATE["last_ai_scan_utc"] = datetime.now(timezone.utc)
            for s in symbols:
                sym = _normalize_symbol(s)
                res = evaluate_checklist(sym)

                if _should_alert(sym, res):
                    send_func(_format_ai_alert(sym, res))
                    SETUP_STATE[sym]["last_alert_utc"] = datetime.now(timezone.utc)

                SETUP_STATE[sym]["last_status"] = res.status
                SETUP_STATE[sym]["last_bias"] = res.bias
                SETUP_STATE[sym]["last_score"] = f"{res.score}/{res.max_score}"

        except Exception as e:
            STATE["last_error"] = f"AI alerts error: {e}"
            print("[TradeWatch] AI alerts error:", e)

        time.sleep(TRADEWATCH_AI_INTERVAL_SEC)

def get_setup_status_text() -> str:
    symbols = TRADEWATCH_SYMBOLS or ["BTCUSDT", "ETHUSDT"]
    lines = ["🧠 [AI Setup Status]"]
    for s in symbols:
        sym = _normalize_symbol(s)
        st = SETUP_STATE.get(sym, {})
        last_status = st.get("last_status", "—")
        last_bias = st.get("last_bias", "—")
        last_score = st.get("last_score", "—")
        last_alert = st.get("last_alert_utc")
        last_alert_str = _iso_or_none(last_alert) if last_alert else "—"
        lines.append(
            f"{sym} — {_status_emoji(last_status)} {last_status} | {_bias_emoji(last_bias)} {last_bias} | {last_score} | last alert: {last_alert_str}"
        )
    return "\n".join(lines)

def classify_trade_style(res: ChecklistResult, atr: float, sl_distance: float) -> str:
    """
    Returns: SCALP | INTRADAY | SWING
    """
    score = res.score
    confirmations = sum([
        res.structure.ok,
        res.liquidity.ok,
        res.fvg.ok
    ])

    if score <= 6 or confirmations <= 1 or sl_distance <= atr * 0.6:
        return "SCALP"

    if score <= 7 or confirmations == 2:
        return "INTRADAY"

    return "SWING"

# =========================
# Main: Trade execution watcher
# =========================

def start_tradewatch(send_func: Callable[[str], None]) -> None:
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
                def _ctime(x: dict) -> int:
                    try:
                        return int(x.get("cTime") or 0)
                    except Exception:
                        return 0

                for f in sorted(fills, key=_ctime):
                    tid = f.get("tradeId") or f.get("id")
                    if not tid:
                        continue
                    tid = str(tid)
                    if tid in seen:
                        continue
                    seen.add(tid)

                    sym = _normalize_symbol(f.get("symbol") or "")
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