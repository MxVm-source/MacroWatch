# bot/modules/market_structure_module.py
"""
MarketStructure — live 4H S/R levels + trendlines + regime + funding/OI broadcast.

Mirrors btc_4h_levels.py (Maxime's local CLI tool) — same fetch, same fractals,
same zone clustering, same trendline fit, same funding+OI logic.

Added for Telegram broadcasting:
  - regime classification (BEAR-RANGE / BEAR-EXP / BULL-RANGE / BULL-EXP / CHOP)
  - format_telegram(struct) — compact broadcast message
  - should_fire(cur, prev) — noise gate (2% proximity OR regime change OR 24h heartbeat)
  - persistent state at /var/data/market_structure_state.json

Schedule: 4H close + 2 min (cron hour="0,4,8,12,16,20", minute=2)
Auto-broadcasts BTC only. ETH available on-demand via /structure ETH.
Fires to PRIVATE group only.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
import numpy as np
import pandas as pd

from bot.utils import send_text

log = logging.getLogger("market_structure")

# ─── Config ───────────────────────────────────────────────────────────────────

# Bitget endpoints (geo-friendly from Render's IPs; Binance returns HTTP 451)
BITGET_BASE     = "https://api.bitget.com"
BITGET_CANDLES  = f"{BITGET_BASE}/api/v2/mix/market/candles"
BITGET_FUNDING  = f"{BITGET_BASE}/api/v2/mix/market/current-fund-rate"
BITGET_FUND_HIST= f"{BITGET_BASE}/api/v2/mix/market/history-fund-rate"
BITGET_OI       = f"{BITGET_BASE}/api/v2/mix/market/open-interest"
PRODUCT_TYPE    = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

NBARS           = int(os.getenv("MS_NBARS", "1000"))
FRACTAL_N       = int(os.getenv("MS_FRACTAL_N", "3"))
ZONE_TOL        = float(os.getenv("MS_ZONE_TOL", "0.006"))      # 0.6% clustering
PROXIMITY_PCT   = float(os.getenv("MS_PROXIMITY_PCT", "0.02"))  # 2% noise gate
HEARTBEAT_HOURS = int(os.getenv("MS_HEARTBEAT_HOURS", "24"))    # min daily fire
TOP_LEVELS      = int(os.getenv("MS_TOP_LEVELS", "3"))

STATE_PATH      = os.getenv("MS_STATE_PATH", "/var/data/market_structure_state.json")

# Persistent state — survives Render redeploys, keyed by symbol
STATE = {}


# ─── Persistent state ─────────────────────────────────────────────────────────

def _load_state():
    global STATE
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        # Convert ISO timestamps back to datetimes (skip OI history lists)
        for sym, s in data.items():
            if sym.startswith("_oi_hist_"):
                continue
            if s.get("last_fire_utc"):
                s["last_fire_utc"] = datetime.fromisoformat(s["last_fire_utc"])
        STATE = data
        log.info(f"MarketStructure: loaded state for {list(STATE.keys())}")
    except FileNotFoundError:
        log.info("MarketStructure: no persisted state file yet")
    except Exception as e:
        log.warning(f"MarketStructure: state load failed: {e}")


def _save_state():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        out = {}
        for sym, s in STATE.items():
            if sym.startswith("_oi_hist_"):
                # Rolling OI history — store as plain list
                out[sym] = s
                continue
            out[sym] = {
                "last_fire_utc":  s["last_fire_utc"].isoformat() if s.get("last_fire_utc") else None,
                "last_regime":    s.get("last_regime"),
                "last_spot":      s.get("last_spot"),
                "last_res_top":   s.get("last_res_top"),
                "last_sup_top":   s.get("last_sup_top"),
            }
        with open(STATE_PATH, "w") as f:
            json.dump(out, f)
    except Exception as e:
        log.warning(f"MarketStructure: state save failed: {e}")


_load_state()


# ─── Data fetch (mirrors btc_4h_levels.py) ────────────────────────────────────

def fetch_klines(sym: str, interval: str = "4H", limit: int = NBARS) -> pd.DataFrame:
    """
    Fetch 4H OHLCV from Bitget USDT perp.
    Bitget v2 caps each request at 200 candles, so we paginate backwards by endTime.
    Response shape: [timestamp, open, high, low, close, base_vol, quote_vol]
    """
    out, end = [], None
    BATCH = 200
    while len(out) < limit:
        params = {
            "symbol":      sym,
            "granularity": interval,
            "limit":       str(min(BATCH, limit - len(out))),
            "productType": PRODUCT_TYPE,
        }
        if end:
            params["endTime"] = str(end)
        r = requests.get(BITGET_CANDLES, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget candles err {data.get('code')}: {data.get('msg')}")
        candles = data.get("data") or []
        if not candles:
            break
        # Bitget returns oldest first per page; prepend to maintain order
        out = candles + out
        # next page: end at the oldest timestamp - 1
        end = int(candles[0][0]) - 1
        time.sleep(0.2)

    df = pd.DataFrame(out, columns=["ot", "o", "h", "l", "c", "v", "qv"])
    df = df[["ot", "o", "h", "l", "c", "v"]].astype(
        {"o": float, "h": float, "l": float, "c": float, "v": float}
    )
    df["ot"] = df["ot"].astype(np.int64)
    df["t"]  = pd.to_datetime(df["ot"], unit="ms", utc=True)
    return df.drop_duplicates("ot").sort_values("ot").reset_index(drop=True)


def fractals(df: pd.DataFrame, n: int):
    """n-bar fractal pivots. Pivot at i confirmed at i+n (anti-lookahead aware)."""
    hi, lo = df["h"].values, df["l"].values
    sh = np.zeros(len(df), bool)
    sl = np.zeros(len(df), bool)
    for i in range(n, len(df) - n):
        if hi[i] == hi[i - n: i + n + 1].max():
            sh[i] = True
        if lo[i] == lo[i - n: i + n + 1].min():
            sl[i] = True
    return sh, sl


def zones(prices, tol: float = ZONE_TOL):
    """Cluster sorted prices within tol% -> list of (mean_price, touch_count)."""
    z = []
    for p in sorted(prices):
        if z and abs(p - z[-1][-1]) / z[-1][-1] < tol:
            z[-1].append(p)
        else:
            z.append([p])
    return [(float(np.mean(c)), len(c)) for c in z]


def fit_tl(idx, prices):
    """Least-squares trendline through swing points."""
    if len(idx) < 2:
        return None
    m, b = np.polyfit(idx, prices, 1)
    return float(m), float(b)


def funding_data(sym: str, limit: int = 42):
    """
    Current funding rate + recent history from Bitget.
    Returns (fr_df with 'fundingRate' column, current_rate_float).
    """
    try:
        cur = requests.get(BITGET_FUNDING,
                           params={"symbol": sym, "productType": PRODUCT_TYPE},
                           timeout=15).json()
        cur_data = (cur.get("data") or [{}])[0]
        cur_rate = float(cur_data.get("fundingRate", 0) or 0)

        hist = requests.get(BITGET_FUND_HIST,
                            params={"symbol": sym, "productType": PRODUCT_TYPE,
                                    "pageSize": str(limit)},
                            timeout=15).json()
        rows = hist.get("data") or []
        fr = pd.DataFrame(rows)
        if not fr.empty and "fundingRate" in fr.columns:
            fr["fundingRate"] = fr["fundingRate"].astype(float)
        else:
            fr = pd.DataFrame({"fundingRate": []})

        return fr, cur_rate
    except Exception as e:
        log.warning(f"funding fetch failed for {sym}: {e}")
        return pd.DataFrame({"fundingRate": []}), 0.0


def open_interest_data(sym: str, period: str = "4h", limit: int = 120):
    """
    Current OI from Bitget. Bitget v2 doesn't expose OI history the way
    Binance does, so we approximate the trend by sampling current OI
    against a stored rolling history kept in STATE.
    """
    try:
        now = requests.get(BITGET_OI,
                           params={"symbol": sym, "productType": PRODUCT_TYPE},
                           timeout=15).json()
        rows = now.get("data") or []
        oi_now = 0.0
        for row in rows:
            if row.get("symbol") == sym:
                oi_now = float(row.get("amount") or row.get("size") or 0)
                break
        if not oi_now and rows:
            oi_now = float(rows[0].get("amount") or rows[0].get("size") or 0)

        # Rolling OI history kept in persistent STATE (bar-by-bar, capped at `limit`)
        hist_key = f"_oi_hist_{sym}"
        oi_hist = STATE.get(hist_key, [])
        oi_hist.append(oi_now)
        oi_hist = oi_hist[-limit:]
        STATE[hist_key] = oi_hist

        oh = pd.DataFrame({"sumOpenInterest": oi_hist})
        return oi_now, oh
    except Exception as e:
        log.warning(f"OI fetch failed for {sym}: {e}")
        return 0.0, pd.DataFrame()


# ─── Regime classifier ───────────────────────────────────────────────────────

def _classify_regime(df: pd.DataFrame) -> str:
    """
    Classify regime from price action over last ~50 bars:
      BULL-EXP   — strong uptrend, above SMA200 and SMA50, SMA50 rising
      BULL-RANGE — above SMA200 but flat
      BEAR-EXP   — strong downtrend, below SMA200 and SMA50, SMA50 falling
      BEAR-RANGE — below SMA200 but flat
      CHOP       — choppy near SMA200, no clear directional bias
    """
    if len(df) < 200:
        return "UNKNOWN"

    close   = df["c"].values
    sma20   = df["c"].rolling(20).mean().iloc[-1]
    sma50   = df["c"].rolling(50).mean().iloc[-1]
    sma200  = df["c"].rolling(200).mean().iloc[-1]
    spot    = close[-1]

    # SMA50 slope: last 10 bars
    sma50_now  = df["c"].rolling(50).mean().iloc[-1]
    sma50_prev = df["c"].rolling(50).mean().iloc[-10]
    slope_pct  = (sma50_now - sma50_prev) / sma50_prev * 100 if sma50_prev else 0

    above_200 = spot > sma200
    above_50  = spot > sma50

    if above_200 and above_50 and slope_pct > 1.5:
        return "BULL-EXP"
    if above_200 and not above_50:
        return "BULL-RANGE"
    if above_200 and above_50:
        return "BULL-RANGE"
    if not above_200 and not above_50 and slope_pct < -1.5:
        return "BEAR-EXP"
    if not above_200 and above_50:
        return "BEAR-RANGE"
    if not above_200 and not above_50:
        return "BEAR-RANGE"
    return "CHOP"


def _trade_zone_verdict(spot: float, res_levels, sup_levels) -> tuple[str, str, float | None]:
    """
    Are we AT a tradeable level (within proximity), or mid-range no-trade?
    Returns (emoji_status, text, near_price_or_None).
    near_price marks which level (if any) the spot is currently sitting at,
    so format_telegram can flag it with "← HERE" without re-deriving it.
    """
    near_res = None
    near_sup = None
    for p, n in res_levels:
        if abs(p - spot) / spot <= PROXIMITY_PCT:
            near_res = (p, n)
            break
    for p, n in sup_levels:
        if abs(p - spot) / spot <= PROXIMITY_PCT:
            near_sup = (p, n)
            break

    if near_res:
        return "✅", f"At resistance {near_res[0]:,.0f} (×{near_res[1]}) — fade or breakout watch", near_res[0]
    if near_sup:
        return "✅", f"At support {near_sup[0]:,.0f} (×{near_sup[1]}) — bounce or breakdown watch", near_sup[0]

    # Mid-range — compute distance to closest level
    candidates = [(p, n, "resistance") for p, n in res_levels] + [(p, n, "support") for p, n in sup_levels]
    if candidates:
        closest = min(candidates, key=lambda x: abs(x[0] - spot))
        return "⏳", f"Mid-range — next level {closest[0]:,.0f} ({closest[2]})", None
    return "⏳", "Mid-range — no nearby levels", None


# ─── Main data function ──────────────────────────────────────────────────────

def get_structure(symbol: str, nbars: int = NBARS, n: int = FRACTAL_N) -> dict:
    """
    Pure data function — pulls everything, returns a dict for formatting or analysis.
    Mirrors btc_4h_levels.py output structure.

    Returns dict with keys:
      symbol, spot, bars, ts, regime,
      res_levels [(price, touches), ...],
      sup_levels [(price, touches), ...],
      funding_now_pct, funding_3d_avg_pct, funding_bias,
      oi_now, oi_trend_pct,
      tl_desc_now, tl_desc_slope, tl_asc_now, tl_asc_slope,
      trade_zone_status, trade_zone_text
    """
    df = fetch_klines(symbol, "4H", nbars)
    sh, sl = fractals(df, n)
    spot = float(df["c"].iloc[-1])
    i_last = len(df) - 1

    sw_hi_idx = df.index[sh].to_numpy()
    sw_hi_p   = df["h"].values[sh]
    sw_lo_idx = df.index[sl].to_numpy()
    sw_lo_p   = df["l"].values[sl]

    res_levels = [(p, c) for p, c in zones(sw_hi_p) if p > spot]
    sup_levels = [(p, c) for p, c in zones(sw_lo_p) if p < spot]
    res_levels.sort(key=lambda x: x[0])           # ascending — closest first
    sup_levels.sort(key=lambda x: -x[0])          # descending — closest first

    # Funding + OI
    fr_df, cur_fr = funding_data(symbol)
    funding_now_pct  = cur_fr * 100
    funding_3d_avg   = (fr_df["fundingRate"].tail(9).mean() * 100) if not fr_df.empty else 0.0
    if cur_fr > 0.0001:
        funding_bias = "crowded LONG"
    elif cur_fr < -0.0001:
        funding_bias = "crowded SHORT"
    else:
        funding_bias = "flat"

    oi_now, oh = open_interest_data(symbol)
    oi_samples = len(oh)
    if oi_samples >= 2:
        oi_trend_pct = (oh["sumOpenInterest"].iloc[-1] / oh["sumOpenInterest"].iloc[0] - 1) * 100
    else:
        oi_trend_pct = 0.0

    # Trendlines (last 3 swings each)
    tl_desc = fit_tl(sw_hi_idx[-3:], sw_hi_p[-3:]) if len(sw_hi_idx) >= 3 else None
    tl_asc  = fit_tl(sw_lo_idx[-3:], sw_lo_p[-3:]) if len(sw_lo_idx) >= 3 else None
    tl_desc_now   = (tl_desc[0] * i_last + tl_desc[1]) if tl_desc else None
    tl_desc_slope = tl_desc[0] if tl_desc else None
    tl_asc_now    = (tl_asc[0] * i_last + tl_asc[1]) if tl_asc else None
    tl_asc_slope  = tl_asc[0] if tl_asc else None

    regime = _classify_regime(df)
    tz_status, tz_text, tz_near_price = _trade_zone_verdict(spot, res_levels, sup_levels)

    return {
        "symbol":              symbol,
        "spot":                spot,
        "bars":                len(df),
        "ts":                  df["t"].iloc[-1],
        "regime":              regime,
        "res_levels":          res_levels[:TOP_LEVELS],
        "sup_levels":          sup_levels[:TOP_LEVELS],
        "funding_now_pct":     funding_now_pct,
        "funding_3d_avg_pct":  funding_3d_avg,
        "funding_bias":        funding_bias,
        "oi_now":              oi_now,
        "oi_trend_pct":        oi_trend_pct,
        "oi_samples":          oi_samples,
        "tl_desc_now":         tl_desc_now,
        "tl_desc_slope":       tl_desc_slope,
        "tl_asc_now":          tl_asc_now,
        "tl_asc_slope":        tl_asc_slope,
        "trade_zone_status":   tz_status,
        "trade_zone_text":     tz_text,
        "trade_zone_near":     tz_near_price,
    }


# ─── Format Telegram message ─────────────────────────────────────────────────

def _fmt_level(p: float, count: int, spot: float, near_price: float | None) -> str:
    """One level line: price, touch count, distance, and HERE marker if it's
    the level the trade-zone verdict is referencing."""
    pct  = (p / spot - 1) * 100
    sign = "+" if pct >= 0 else ""
    here = "  ← HERE" if (near_price is not None and abs(p - near_price) < 1e-6) else ""
    return f"  {p:>10,.0f}  ×{count}  ({sign}{pct:.1f}%){here}"


_REGIME_EMOJI = {
    "BULL-EXP":   "🟢",
    "BULL-RANGE": "🟡",
    "BEAR-EXP":   "🔴",
    "BEAR-RANGE": "🟡",
    "CHOP":       "⚪",
    "UNKNOWN":    "⚪",
}


def format_telegram(s: dict, trigger_line: str | None = None) -> str:
    """
    Build the broadcast message — verdict-first, scannable level lists.

    trigger_line: optional pre-computed CVD gate verdict line from
    trigger.format_trigger_line(). Passed in (rather than computed here)
    so format_telegram stays a pure formatter — the caller decides whether
    to run the CVD gate (extra network call) and handles its failure mode.
    """
    sym        = s["symbol"]
    spot       = s["spot"]
    regime     = s["regime"]
    ts_str     = s["ts"].strftime("%d-%b %H:%M UTC")
    near_price = s.get("trade_zone_near")
    regime_emoji = _REGIME_EMOJI.get(regime, "⚪")

    AGGR_URL = "https://aggr.trade/6zn3"

    lines = [
        f"📊 *{sym} 4H* — `${spot:,.0f}`",
        f"🕐 {ts_str}",
        "",
        f"{regime_emoji} *{regime}*",
        f"{s['trade_zone_status']} {s['trade_zone_text']}",
    ]

    # CVD gate verdict (if provided) replaces the generic aggr nudge —
    # it tells you what the gate concluded, not just "go check".
    if trigger_line:
        lines.append("")
        lines.append(trigger_line)
    elif s["trade_zone_status"] == "✅":
        lines.append(f"   → [Check aggr 15m CVD]({AGGR_URL})")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Resistance block
    if s["res_levels"]:
        lines.append("🔴 *RESISTANCE*")
        for p, c in s["res_levels"]:
            lines.append(_fmt_level(p, c, spot, near_price))
        lines.append("")

    # Support block
    if s["sup_levels"]:
        lines.append("🟢 *SUPPORT*")
        for p, c in s["sup_levels"]:
            lines.append(_fmt_level(p, c, spot, near_price))
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    # Trendlines — plain English
    tl_lines = []
    if s["tl_desc_now"]:
        tl_lines.append(f"  Descending cap:  ${s['tl_desc_now']:,.0f}")
    if s["tl_asc_now"]:
        tl_lines.append(f"  Ascending base:  ${s['tl_asc_now']:,.0f}")
    if tl_lines:
        lines.append("📐 *Trendlines*")
        lines += tl_lines
        lines.append("")

    # Funding + OI — separate lines
    f_sign  = "+" if s["funding_now_pct"] >= 0 else ""
    oi_sign = "+" if s["oi_trend_pct"] >= 0 else ""
    lines.append(f"📡 Funding: {f_sign}{s['funding_now_pct']:.4f}%/8h ({s['funding_bias']})")

    oi_samples = s.get("oi_samples", 0)
    if oi_samples < 20:
        lines.append(f"📊 OI trend: building history ({oi_samples}/120 bars)")
    else:
        lines.append(f"📊 OI trend: {oi_sign}{s['oi_trend_pct']:.1f}% ({oi_samples} bars)")

    return "\n".join(lines)


# ─── Noise gate ──────────────────────────────────────────────────────────────

def should_fire(cur: dict, prev: dict | None) -> tuple[bool, str]:
    """
    Decide whether to broadcast this snapshot.

    Fires when ANY of:
      1. Proximity: price within PROXIMITY_PCT of nearest level
      2. State change: regime flipped, OR top resistance/support changed
      3. Heartbeat: HEARTBEAT_HOURS elapsed since last fire

    Returns (should_fire, reason).
    """
    # Trigger 1 — proximity
    spot = cur["spot"]
    for p, _ in cur["res_levels"] + cur["sup_levels"]:
        if abs(p - spot) / spot <= PROXIMITY_PCT:
            return True, f"proximity to {p:,.0f}"

    # Trigger 2 — state change
    if prev:
        if cur["regime"] != prev.get("last_regime"):
            return True, f"regime flip ({prev.get('last_regime')} → {cur['regime']})"
        cur_res_top = cur["res_levels"][0][0] if cur["res_levels"] else None
        cur_sup_top = cur["sup_levels"][0][0] if cur["sup_levels"] else None
        if prev.get("last_res_top") and cur_res_top:
            if abs(cur_res_top - prev["last_res_top"]) / prev["last_res_top"] > 0.005:
                return True, "resistance shifted"
        if prev.get("last_sup_top") and cur_sup_top:
            if abs(cur_sup_top - prev["last_sup_top"]) / prev["last_sup_top"] > 0.005:
                return True, "support shifted"

    # Trigger 3 — heartbeat
    last_fire = prev.get("last_fire_utc") if prev else None
    if not last_fire:
        return True, "first fire"
    if datetime.now(timezone.utc) - last_fire >= timedelta(hours=HEARTBEAT_HOURS):
        return True, "heartbeat"

    return False, "no change"


# ─── Job entry points ────────────────────────────────────────────────────────

def _get_trigger_line(struct: dict, symbol: str) -> str | None:
    """
    Run the CVD gate (trigger.evaluate) and return its formatted line.
    Isolated try/except — a CVD/network failure must never break the
    underlying structure broadcast, just omit this line.
    """
    try:
        from bot.modules.trigger import evaluate, format_trigger_line
        verdict = evaluate(struct, symbol=symbol)
        if verdict["cvd"].get("direction") == "unavailable":
            return None
        return format_trigger_line(verdict)
    except Exception as e:
        log.warning(f"CVD trigger gate failed for {symbol}: {e}")
        return None


def poll_and_maybe_fire(symbol: str = "BTCUSDT"):
    """Pull structure, apply noise gate, fire to private if triggered."""
    try:
        struct = get_structure(symbol)
        prev   = STATE.get(symbol, {})
        fire, reason = should_fire(struct, prev)

        log.info(f"MarketStructure {symbol}: regime={struct['regime']} fire={fire} ({reason})")

        if fire:
            trigger_line = _get_trigger_line(struct, symbol)
            msg = format_telegram(struct, trigger_line=trigger_line)
            send_text(msg)

            # Save state
            STATE[symbol] = {
                "last_fire_utc":  datetime.now(timezone.utc),
                "last_regime":    struct["regime"],
                "last_spot":      struct["spot"],
                "last_res_top":   struct["res_levels"][0][0] if struct["res_levels"] else None,
                "last_sup_top":   struct["sup_levels"][0][0] if struct["sup_levels"] else None,
            }
            _save_state()
    except Exception as e:
        log.exception(f"MarketStructure poll failed for {symbol}: {e}")


def poll_all():
    """Called by the scheduler. BTC only — ETH remains available on-demand
    via /structure ETH but is not auto-broadcast."""
    poll_and_maybe_fire("BTCUSDT")


# ─── Command entrypoint ──────────────────────────────────────────────────────

def show_structure(symbol: str = "BTC"):
    """Handler for /structure [BTC|ETH] — fire immediately regardless of gate."""
    sym = symbol.upper().strip()
    if not sym.endswith("USDT"):
        sym = sym + "USDT"
    if sym not in ("BTCUSDT", "ETHUSDT"):
        send_text(f"📊 [MarketStructure] Unknown symbol: {sym}. Use BTC or ETH.")
        return

    try:
        struct = get_structure(sym)
        trigger_line = _get_trigger_line(struct, sym)
        msg = format_telegram(struct, trigger_line=trigger_line)
        send_text(msg)
    except Exception as e:
        log.exception(f"show_structure failed: {e}")
        send_text(f"📊 [MarketStructure] Error: {str(e)[:200]}")
