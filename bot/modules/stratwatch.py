# bot/modules/stratwatch.py
"""
StratWatch — ATRb v2 Strategy Status

Command: /status
Shows live strategy state for ETH including:
  - Bot status + account balance
  - Per-asset: price, 4H MACD, ATR%, regime, open position
  - Next 4H candle close (cycle time)
  - Entry condition summary per asset
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text
from bot.datafeed_bitget import (
    _fetch_current_futures_position,
    _position_is_open,
    _to_float,
    BITGET_BASE_URL,
    BITGET_PRODUCT_TYPE,
)

log = logging.getLogger("stratwatch")

# ─── Strategy config ─────────────────────────────────────────────────────────

ASSETS = [
    {"symbol": "ETHUSDT", "ticker": "ETH", "weight": "100%"},
]

ATR_PERIOD    = 14
ATR_MULT      = 1.0   # regime threshold
ATR_HOT       = 4.0   # % — entry requires above this
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
CANDLE_TF     = "4H"
CANDLE_LIMIT  = 60    # enough for MACD + ATR

# ─── Bitget candle fetch ──────────────────────────────────────────────────────

def _fetch_candles(symbol: str, limit: int = CANDLE_LIMIT) -> list:
    """Fetch 4H OHLCV from Bitget public API. Returns list of [ts,o,h,l,c,vol]."""
    try:
        r = requests.get(
            f"{BITGET_BASE_URL}/api/v2/mix/market/candles",
            params={
                "symbol":      symbol,
                "granularity": "4H",
                "limit":       str(limit),
                "productType": BITGET_PRODUCT_TYPE,
            },
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            log.warning(f"Candle fetch error for {symbol}: {data.get('msg')}")
            return []
        return data.get("data") or []
    except Exception as e:
        log.warning(f"Candle fetch failed for {symbol}: {e}")
        return []


# ─── Indicator calculations ───────────────────────────────────────────────────

def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _compute_macd(closes: list) -> dict | None:
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return None
    fast = _ema(closes, MACD_FAST)
    slow = _ema(closes, MACD_SLOW)

    # Align lengths
    offset = len(fast) - len(slow)
    fast   = fast[offset:]
    macd_line = [f - s for f, s in zip(fast, slow)]

    if len(macd_line) < MACD_SIGNAL:
        return None

    signal_line = _ema(macd_line, MACD_SIGNAL)
    offset2     = len(macd_line) - len(signal_line)
    macd_line   = macd_line[offset2:]
    histogram   = [m - s for m, s in zip(macd_line, signal_line)]

    return {
        "macd":      round(macd_line[-1], 4),
        "signal":    round(signal_line[-1], 4),
        "histogram": round(histogram[-1], 4),
        "prev_hist": round(histogram[-2], 4) if len(histogram) >= 2 else 0,
    }


def _compute_atr(candles: list, period: int = ATR_PERIOD) -> dict | None:
    if len(candles) < period + 1:
        return None
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]

    trs = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1]),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    atr_abs = sum(trs[-period:]) / period
    price   = closes[-1]
    atr_pct = round(atr_abs / price * 100, 2) if price else 0

    # Regime: current ATR vs ATR 30 bars ago
    atr_now  = atr_abs
    atr_prev = sum(trs[-period - ATR_PERIOD:-ATR_PERIOD]) / period if len(trs) >= period * 2 else atr_abs
    expanding = atr_now >= atr_prev * ATR_MULT

    return {
        "atr_pct":   atr_pct,
        "expanding": expanding,
    }


# ─── Next 4H cycle ────────────────────────────────────────────────────────────

def _next_4h_utc() -> tuple[str, int]:
    now        = datetime.now(timezone.utc)
    # Round down to current 4H block, then add 4H
    current_block = (now.hour // 4) * 4
    next_cycle = now.replace(hour=current_block, minute=0, second=0, microsecond=0) \
                 + timedelta(hours=4)
    mins_left  = max(0, int((next_cycle - now).total_seconds() / 60))
    return next_cycle.strftime("%H:%M UTC"), mins_left


# ─── Per-asset analysis ───────────────────────────────────────────────────────

def _analyse_asset(asset: dict) -> dict:
    symbol  = asset["symbol"]
    ticker  = asset["ticker"]
    candles = _fetch_candles(symbol)

    result = {
        "ticker":     ticker,
        "weight":     asset["weight"],
        "symbol":     symbol,
        "price":      None,
        "macd":       None,
        "atr":        None,
        "position":   None,
        "error":      None,
    }

    if not candles:
        result["error"] = "No data"
        return result

    try:
        closes = [float(c[4]) for c in candles]
        result["price"] = closes[-1]

        result["macd"] = _compute_macd(closes)
        result["atr"]  = _compute_atr(candles)

        pos = _fetch_current_futures_position(symbol)
        if _position_is_open(pos):
            side   = (pos.get("holdSide") or "").upper()
            entry  = _to_float(pos.get("openPriceAvg") or pos.get("openPrice"))
            size   = _to_float(pos.get("total") or pos.get("available"))
            upnl   = _to_float(pos.get("unrealizedPL") or pos.get("upl"))
            lev    = pos.get("leverage", "?")
            result["position"] = {
                "side":  side,
                "entry": entry,
                "size":  size,
                "upnl":  upnl,
                "lev":   lev,
            }
    except Exception as e:
        result["error"] = str(e)[:60]

    return result


# ─── Entry condition check ────────────────────────────────────────────────────

def _entry_conditions(analysis: dict) -> tuple[str, str]:
    """Returns (status_emoji, condition_text) for each asset."""
    macd = analysis.get("macd")
    atr  = analysis.get("atr")
    pos  = analysis.get("position")

    if pos:
        side_emoji = "🟢" if pos["side"] == "LONG" else "🔴"
        upnl_sign  = "+" if pos["upnl"] >= 0 else ""
        return (
            side_emoji,
            f"IN TRADE {side_emoji}  Entry: {pos['entry']:.2f}  "
            f"uPnL: {upnl_sign}{pos['upnl']:.2f} USDT  {pos['lev']}x"
        )

    if not macd or not atr:
        return "⚠️", "Data unavailable"

    macd_bull  = macd["macd"] > macd["signal"]
    hist_rising = macd["histogram"] > macd["prev_hist"]
    atr_ok     = atr["atr_pct"] >= ATR_HOT
    expanding  = atr["expanding"]

    checks = [macd_bull, hist_rising, atr_ok, expanding]
    score  = sum(checks)

    if score == 4:
        return "🟢", "All conditions met — entry possible"
    elif score == 3:
        return "🟡", "Near entry — 1 condition missing"
    elif score <= 1:
        return "⚪", "Cooling — waiting for setup"
    else:
        return "🟡", "Watching — 2 conditions pending"


# ─── Message builder ─────────────────────────────────────────────────────────

def build_status() -> str:
    now              = datetime.now(timezone.utc)
    next_cycle, mins = _next_4h_utc()

    # Account balance — ATRb v2 lives on the BITGET_API_KEY account
    # (currently the sub-account, eventually sub/elite). Never read Elite here —
    # Elite is Maxime's discretionary LIVE Trading book, separate product.
    balance_str = "—"
    try:
        from bot.datafeed_bitget import _signed_request, BITGET_PRODUCT_TYPE as _PT
        res = _signed_request("GET", "/api/v2/mix/account/accounts",
                              params={"productType": _PT, "marginCoin": "USDT"})
        accounts = res.get("data") or []
        if isinstance(accounts, dict):
            accounts = [accounts]
        bal = None
        for acc in accounts:
            coin = (acc.get("marginCoin") or acc.get("coin") or "").upper()
            if coin == "USDT":
                bal = round(float(acc.get("usdtEquity") or acc.get("available") or 0), 2)
                break
        if bal is not None:
            balance_str = f"${bal:,.2f}"
    except Exception as e:
        log.warning(f"StratWatch balance fetch failed: {e}")

    lines = [
        "🤖 *ATRb v2 — Live Status*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Bot: 🟢 Live  |  Balance: `{balance_str}`",
        f"Assets: ETH 100%",
        f"Next cycle: {next_cycle}  _(in {mins}m)_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    any_position = False

    for asset in ASSETS:
        a   = _analyse_asset(asset)
        pos = a.get("position")
        if pos:
            any_position = True

        macd = a.get("macd")
        atr  = a.get("atr")

        # Price
        price_str = f"`@ ${a['price']:,.2f}`" if a["price"] else "`—`"

        # MACD line
        if macd:
            hist    = macd["histogram"]
            rising  = hist > macd["prev_hist"]
            bull    = macd["macd"] > macd["signal"]
            if bull and rising:
                m_emoji = "📈"
            elif bull and not rising:
                m_emoji = "🟡"   # bullish but momentum fading
            elif not bull and not rising:
                m_emoji = "📉"
            else:
                m_emoji = "🟡"   # bearish but momentum recovering
            macd_str = f"`{macd['macd']:+.4f}`  {m_emoji}"
        else:
            macd_str = "`—`"

        # ATR line
        if atr:
            a_emoji  = "🔥" if atr["atr_pct"] >= ATR_HOT else "❄️"
            exp_str  = "expanding ↑" if atr["expanding"] else "flat →"
            atr_str  = f"`{atr['atr_pct']:.2f}%`  {a_emoji}  {exp_str}"
        else:
            atr_str = "`—`"

        # Condition
        cond_emoji, cond_text = _entry_conditions(a)

        lines += [
            "",
            f"*{a['ticker']}*  {a['weight']}  {price_str}",
            f"  MACD:  {macd_str}",
            f"  ATR%:  {atr_str}",
            f"  {cond_emoji}  {cond_text}",
        ]

        if a.get("error"):
            lines.append(f"  ⚠️ _{a['error']}_")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if any_position:
        lines.append("_Positions live — monitoring active_")
    else:
        lines.append("_Scanning every 4H — no position open_")

    return "\n".join(lines)


# ─── Entry point ─────────────────────────────────────────────────────────────

def show_status():
    try:
        msg = build_status()
    except Exception as e:
        log.exception(f"StratWatch build_status failed: {e}")
        msg = f"🤖 [StratWatch] ⚠️ Status build failed: {str(e)[:200]}"
    send_text(msg)
