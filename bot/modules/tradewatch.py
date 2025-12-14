#!/usr/bin/env python3
"""
TradeWatch (Bitget) + AI Checklist Integration
==============================================

What this module does:
- Poll Bitget fills (default: Spot fills endpoint) and emit Telegram messages via a provided send_func(text).
- Provide a /tradewatch_status-readable status string.
- Provide an AI checklist evaluation based on 4H candles:
    - check_structure()  (HH/HL vs LH/LL + optional EMA200 alignment)
    - check_liquidity()  (sweep + reclaim)
    - check_fvg()        (ICT-style 3-candle FVG touch + reaction)

How to use (typical):
    from tradewatch import start_tradewatch, get_status, get_checklist_status_text

    # in your bot code:
    threading.Thread(target=start_tradewatch, args=(send_text,), daemon=True).start()

    # command handlers:
    # /tradewatch_status -> get_status()
    # /checklist BTCUSDT -> get_checklist_status_text("BTCUSDT")

Environment variables:
- BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE (required)
- TRADEWATCH_ENABLED=1 (required to run watcher)
- TRADEWATCH_SYMBOL="BTCUSDT" (optional filter; default: all)
- TRADEWATCH_POLL_INTERVAL_SEC=10 (default 10)
- TRADEWATCH_DEGEN=1 (optional funny templates)

Candle endpoint note:
Bitget has multiple candle endpoints (mix/spot). We try MIX first then SPOT fallback.
If your account requires productType or uses a different endpoint, adjust fetch_candles_4h().
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
TRADEWATCH_SYMBOL = os.environ.get("TRADEWATCH_SYMBOL", "")  # optional filter: "BTCUSDT" etc.
TRADEWATCH_POLL_INTERVAL_SEC = int(os.environ.get("TRADEWATCH_POLL_INTERVAL_SEC", "10"))

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
    "ð Admin yeeted into a trade!",
    "ð Admin just sent it.",
    "ð§¨ Admin deployed capital irresponsibly.",
    "ð¥ Position opened â cope accordingly.",
    "ð¦ Big ape energy detected.",
]

DEGEN_CLOSE = [
    "ð¼ Trade closed â consequences unknown.",
    "ð Exit deployed (survivedâ¦ barely).",
    "ðª¦ Position closed â funeral avoided.",
    "ð¸ Trade ended â PnL prayed for.",
]

# =========================
# Helpers
# =========================

def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _iso_or_none(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "â"

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
# Fills (TradeWatch)
# =========================

def _fetch_spot_fills(limit: int = 50) -> List[dict]:
    """
    Bitget Spot V2: Get Fills
      GET /api/v2/spot/trade/fills
    """
    params: Dict[str, str] = {"limit": str(limit)}
    if TRADEWATCH_SYMBOL:
        params["symbol"] = TRADEWATCH_SYMBOL

    data = _signed_request("GET", "/api/v2/spot/trade/fills", params=params, body=None)
    return data.get("data", []) or []

def _classify_execution(fill: dict) -> str:
    """
    Best-effort classification based on Bitget fields.
    Works with spot fills; futures fills may differ.
    """
    scope = (fill.get("tradeScope") or "").lower()
    order_type = (fill.get("orderType") or "").lower()

    if "take" in scope or "tp" in scope or "take_profit" in scope:
        return "Take Profit"
    if "stop" in scope or "sl" in scope or "stop_loss" in scope:
        return "Stop Loss"
    if "close" in scope or "reduce" in scope or "reduce" in order_type:
        return "Position Close/Reduce"

    # Spot fills are buys/sells (not positions), but your Telegram wants "Position Executed"
    return "Position Open/Increase"

def _format_message(fill: dict, checklist_block: str | None = None) -> str:
    """
    Telegram message format.
    """
    pair = fill.get("symbol", "N/A")
    side_raw = (fill.get("side") or "N/A").upper()
    entry = fill.get("priceAvg") or fill.get("price") or "N/A"
    size = fill.get("size") or fill.get("amount") or "N/A"

    if TRADEWATCH_DEGEN:
        header = random.choice(DEGEN_OPEN if side_raw in ("BUY", "SELL") else DEGEN_CLOSE)
    else:
        header = "ð [TradeWatch] New Execution"

    if side_raw == "BUY":
        side_emoji = "ð¢"
    elif side_raw == "SELL":
        side_emoji = "ð´"
    else:
        side_emoji = "ð"

    execution = _classify_execution(fill)

    msg = (
        f"{side_emoji} {header}\n"
        f"Pair: {pair}\n"
        f"Side: {side_raw}\n"
        f"Price: {entry}\n"
        f"Size: {size}\n"
        f"Execution: {execution}\n"
        f"Time (UTC): {_iso_utc_now()}"
    )

    if checklist_block:
        msg += "\n\n" + checklist_block

    return msg

def get_status() -> str:
    enabled = TRADEWATCH_ENABLED
    symbol = TRADEWATCH_SYMBOL or "ALL"
    lines = [
        "ð [TradeWatch] Status",
        f"Enabled: {'Yes â' if enabled else 'No â'}",
        f"Symbol filter: {symbol}",
        f"Running loop: {'Yes â' if STATE['running'] else 'No â'}",
        f"Last poll (UTC): {_iso_or_none(STATE['last_poll_utc'])}",
        f"Last trade (UTC): {_iso_or_none(STATE['last_trade_utc'])}",
    ]
    if STATE.get("last_trade_pair"):
        lines.append(f"Last trade: {STATE['last_trade_pair']} {STATE.get('last_trade_side','')}".strip())
    if STATE.get("last_checklist_status"):
        lines.append(f"Last checklist: {STATE.get('last_checklist_symbol','?')} â {STATE['last_checklist_status']} ({_iso_or_none(STATE['last_checklist_utc'])})")
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

    print("[TradeWatch] Watcher started...")
    STATE["running"] = True

    seen: set[str] = set()
    first_run = True

    while True:
        try:
            STATE["last_poll_utc"] = datetime.now(timezone.utc)

            fills = _fetch_spot_fills(limit=50)

            if first_run:
                # mark existing as seen to avoid spam
                for f in fills:
                    tid = f.get("tradeId") or f.get("fillId") or f.get("id")
                    if tid:
                        seen.add(str(tid))
                first_run = False
            else:
                # send oldest unseen first
                for f in reversed(fills):
                    tid = f.get("tradeId") or f.get("fillId") or f.get("id")
                    if not tid:
                        continue
                    tid = str(tid)
                    if tid in seen:
                        continue

                    seen.add(tid)

                    # Update status
                    STATE["last_trade_utc"] = datetime.now(timezone.utc)
                    STATE["last_trade_pair"] = f.get("symbol")
                    STATE["last_trade_side"] = (f.get("side") or "").upper()
                    STATE["last_error"] = None

                    checklist_block = None
                    if TRADEWATCH_CHECKLIST_ENABLED and f.get("symbol"):
                        try:
                            checklist_block = get_checklist_status_text(f.get("symbol"), include_reasons=False)
                        except Exception as ce:
                            # don't fail trade alerts if checklist fails
                            checklist_block = f"ð§  Checklist: unavailable ({ce})"

                    msg = _format_message(f, checklist_block=checklist_block)
                    send_func(msg)

                # Avoid unbounded growth
                if len(seen) > 3000:
                    seen = set(list(seen)[-2000:])

        except Exception as e:
            STATE["last_error"] = str(e)
            print("[TradeWatch] Error:", e)

        time.sleep(TRADEWATCH_POLL_INTERVAL_SEC)

# ============================================================
# AI Checklist (Structure / Liquidity / FVG) â 4H candles
# ============================================================

Candle = Dict[str, float]

@dataclass
class ChecklistResult:
    status: str             # â / ð¡ / ð´
    bias: str               # LONG / SHORT / NEUTRAL
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
    bias: str  # "LONG" / "SHORT" / "NEUTRAL"
    reasons: List[str]
    details: Dict[str, object]

def bitget_klines_to_candles(raw: Any) -> List[Candle]:
    """
    Convert Bitget candle responses into:
      [{"ts","open","high","low","close","volume"}, ...]
    Supports list-of-lists or list-of-dicts under raw["data"].
    """
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
    """
    Try MIX 4H candles, then SPOT fallback.
    If your MIX endpoint requires productType, add it in params below.
    """
    # MIX (futures) - attempt
    try:
        raw = _signed_request(
            "GET",
            "/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": "4H", "limit": str(limit)},
            body=None,
        )
        candles = bitget_klines_to_candles(raw)
        if candles:
            return candles
    except Exception:
        pass

    # SPOT fallback
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
    """
    Score out of 4:
      - 2 points for pivot structure (HH/HL or LH/LL)
      - 2 points for EMA alignment (close above/below EMA200)
    """
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
    return CheckResult(ok, score, 4, bias, reasons, {"close": last_close, "ema": last_ema, "highs": highs, "lows": lows})

def check_liquidity(candles: List[Candle], lookback: int = 24, reclaim_required: bool = True) -> CheckResult:
    """
    Score out of 3:
      - 1 point: sweep detected (beyond recent high/low + margin)
      - 2 points: reclaim confirmed (if reclaim_required)
    """
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
    return CheckResult(ok, score, 3, bias, reasons, {"recent_high": recent_high, "recent_low": recent_low, "margin": margin, "atr": atr})

def check_fvg(candles: List[Candle], max_lookback: int = 80) -> CheckResult:
    """
    ICT-style 3-candle FVG detection. Score out of 3:
      1 = FVG touched by last candle
      1 = reaction wick
      1 = close confirms direction vs midpoint
    """
    if len(candles) < 10:
        return CheckResult(False, 0, 3, "NEUTRAL", ["Not enough candles for FVG check."], {})

    atr = _atr(candles, 14)
    min_gap = atr * 0.05 if atr > 0 else 0.0

    zones: List[Tuple[str, float, float, int]] = []
    start = max(2, len(candles) - max_lookback)
    for i in range(start, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]
        # bullish gap up: c1.high < c3.low
        if c1["high"] + min_gap < c3["low"]:
            zones.append(("bullish", c1["high"], c3["low"], i))
        # bearish gap down: c1.low > c3.high
        if c1["low"] - min_gap > c3["high"]:
            zones.append(("bearish", c3["high"], c1["low"], i))

    if not zones:
        return CheckResult(False, 0, 3, "NEUTRAL", ["No recent FVG found."], {})

    kind, z_low, z_high, created_i = zones[-1]
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

    return CheckResult(score >= 2, score, 3, bias, reasons, {"zone": (kind, z_low, z_high), "mid": mid, "atr": atr})

def evaluate_checklist(symbol: str) -> ChecklistResult:
    candles = fetch_candles_4h(symbol)
    if not candles:
        s = CheckResult(False, 0, 4, "NEUTRAL", ["No candles returned from Bitget."], {})
        l = CheckResult(False, 0, 3, "NEUTRAL", ["No candles returned from Bitget."], {})
        f = CheckResult(False, 0, 3, "NEUTRAL", ["No candles returned from Bitget."], {})
        return ChecklistResult("ð´ NO DATA", "NEUTRAL", 0, 10, s, l, f)

    s = check_structure(candles)
    l = check_liquidity(candles)
    f = check_fvg(candles)

    structure_ok = s.ok and s.bias in ("LONG", "SHORT")
    setup_ok = ((l.ok and l.bias == s.bias) or (f.ok and f.bias == s.bias))

    score = s.score + l.score + f.score
    max_score = s.max_score + l.max_score + f.max_score

    if structure_ok and setup_ok:
        status = "â SETUP VALID"
        bias = s.bias
    elif structure_ok:
        status = "ð¡ PARTIAL (WAIT)"
        bias = s.bias
    else:
        status = "ð´ NO TRADE"
        bias = "NEUTRAL"

    # update STATE memory for /tradewatch_status
    STATE["last_checklist_utc"] = datetime.now(timezone.utc)
    STATE["last_checklist_symbol"] = symbol
    STATE["last_checklist_status"] = status

    return ChecklistResult(status, bias, score, max_score, s, l, f)

def get_checklist_status_text(symbol: str, include_reasons: bool = True) -> str:
    """
    Returns a Telegram-ready checklist block.
    """
    res = evaluate_checklist(symbol)

    lines = [
        "ð§  [AI Checklist]",
        f"Symbol: {symbol}",
        f"Status: {res.status}",
        f"Bias: {res.bias}",
        f"Score: {res.score}/{res.max_score}",
    ]

    if include_reasons:
        lines.append("")
        lines.append("Structure:")
        for r in res.structure.reasons[:5]:
            lines.append(f"â¢ {r}")
        lines.append("Liquidity:")
        for r in res.liquidity.reasons[:5]:
            lines.append(f"â¢ {r}")
        lines.append("FVG:")
        for r in res.fvg.reasons[:5]:
            lines.append(f"â¢ {r}")

    return "\n".join(lines)
