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

FAPI            = "https://fapi.binance.com/fapi/v1/klines"
FAPI_FUNDING    = "https://fapi.binance.com/fapi/v1/fundingRate"
FAPI_PREMIUM    = "https://fapi.binance.com/fapi/v1/premiumIndex"
FAPI_OI         = "https://fapi.binance.com/fapi/v1/openInterest"
FAPI_OI_HIST    = "https://fapi.binance.com/futures/data/openInterestHist"

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
        # Convert ISO timestamps back to datetimes
        for sym, s in data.items():
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

def fetch_klines(sym: str, interval: str = "4h", limit: int = NBARS) -> pd.DataFrame:
    """Fetch OHLCV bars from Binance USDT-M perp, paginated backwards if needed."""
    out, end = [], None
    while len(out) < limit:
        p = {"symbol": sym, "interval": interval, "limit": min(1000, limit - len(out))}
        if end:
            p["endTime"] = end
        r = requests.get(FAPI, params=p, timeout=15)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        out = k + out
        end = k[0][0] - 1
        time.sleep(0.2)
    df = pd.DataFrame(out, columns=["ot","o","h","l","c","v","ct","q","n","tb","tq","ig"])
    df = df[["ot","o","h","l","c","v"]].astype(
        {"o": float, "h": float, "l": float, "c": float, "v": float}
    )
    df["t"] = pd.to_datetime(df["ot"], unit="ms", utc=True)
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
    """Last N funding prints (8h cadence) + current premium index rate."""
    try:
        r = requests.get(FAPI_FUNDING, params={"symbol": sym, "limit": limit}, timeout=15)
        r.raise_for_status()
        fr = pd.DataFrame(r.json())
        fr["fundingRate"] = fr["fundingRate"].astype(float)
        cur = requests.get(FAPI_PREMIUM, params={"symbol": sym}, timeout=15).json()
        return fr, float(cur["lastFundingRate"])
    except Exception as e:
        log.warning(f"funding fetch failed for {sym}: {e}")
        return pd.DataFrame(), 0.0


def open_interest_data(sym: str, period: str = "4h", limit: int = 120):
    """Current OI + 4H history for trend computation."""
    try:
        now = requests.get(FAPI_OI, params={"symbol": sym}, timeout=15).json()
        hist = requests.get(FAPI_OI_HIST,
                            params={"symbol": sym, "period": period, "limit": limit},
                            timeout=15)
        oh = pd.DataFrame(hist.json()) if hist.ok else pd.DataFrame()
        if not oh.empty:
            oh["sumOpenInterest"] = oh["sumOpenInterest"].astype(float)
        return float(now["openInterest"]), oh
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


def _trade_zone_verdict(spot: float, res_levels, sup_levels) -> tuple[str, str]:
    """
    Are we AT a tradeable level (within proximity), or mid-range no-trade?
    Returns (emoji_status, text).
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
        return "✅", f"At resistance {near_res[0]:,.0f} (x{near_res[1]}) — fade/breakout zone"
    if near_sup:
        return "✅", f"At support {near_sup[0]:,.0f} (x{near_sup[1]}) — bounce/breakdown zone"

    # Mid-range — compute distance to closest level
    candidates = [(p, n, "R") for p, n in res_levels] + [(p, n, "S") for p, n in sup_levels]
    if candidates:
        closest = min(candidates, key=lambda x: abs(x[0] - spot))
        return "❌", f"Mid-range — wait for {closest[0]:,.0f} ({closest[2]})"
    return "❌", "No nearby levels"


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
    df = fetch_klines(symbol, "4h", nbars)
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
    if not oh.empty:
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
    tz_status, tz_text = _trade_zone_verdict(spot, res_levels, sup_levels)

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
        "tl_desc_now":         tl_desc_now,
        "tl_desc_slope":       tl_desc_slope,
        "tl_asc_now":          tl_asc_now,
        "tl_asc_slope":        tl_asc_slope,
        "trade_zone_status":   tz_status,
        "trade_zone_text":     tz_text,
    }


# ─── Format Telegram message ─────────────────────────────────────────────────

def _fmt_level(p: float, count: int, spot: float, mark_near: bool = True) -> str:
    pct = (p / spot - 1) * 100
    sign = "+" if pct >= 0 else ""
    near = ""
    if mark_near and abs(pct) <= PROXIMITY_PCT * 100:
        near = " ← NEAR"
    return f"{p:,.0f} ×{count} ({sign}{pct:.1f}%){near}"


def format_telegram(s: dict) -> str:
    """Build the compact broadcast message."""
    sym       = s["symbol"]
    spot      = s["spot"]
    regime    = s["regime"]
    ts_str    = s["ts"].strftime("%d-%b %H:%M UTC")

    # Resistance line
    res_parts = [_fmt_level(p, c, spot) for p, c in s["res_levels"]]
    res_str   = " | ".join(res_parts) if res_parts else "—"

    # Support line
    sup_parts = [_fmt_level(p, c, spot) for p, c in s["sup_levels"]]
    sup_str   = " | ".join(sup_parts) if sup_parts else "—"

    # Trendlines
    tl_parts = []
    if s["tl_desc_now"]:
        tl_parts.append(f"desc: {s['tl_desc_now']:,.0f}")
    if s["tl_asc_now"]:
        tl_parts.append(f"asc: {s['tl_asc_now']:,.0f}")
    tl_str = " | ".join(tl_parts) if tl_parts else "—"

    # Funding + OI
    f_sign = "+" if s["funding_now_pct"] >= 0 else ""
    oi_sign = "+" if s["oi_trend_pct"] >= 0 else ""
    funding_oi = (f"Funding: {f_sign}{s['funding_now_pct']:.4f}%/8h {s['funding_bias']} | "
                  f"OI: {oi_sign}{s['oi_trend_pct']:.1f}% (120 bars)")

    lines = [
        f"📊 *{sym} 4H* | `{spot:,.0f}` | {ts_str}",
        f"Regime: *{regime}* | {s['trade_zone_status']} {s['trade_zone_text']}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"R: {res_str}",
        f"S: {sup_str}",
        f"TL {tl_str}",
        funding_oi,
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Trade zone: {s['trade_zone_status']} {s['trade_zone_text']}",
    ]
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

def poll_and_maybe_fire(symbol: str = "BTCUSDT"):
    """Pull structure, apply noise gate, fire to private if triggered."""
    try:
        struct = get_structure(symbol)
        prev   = STATE.get(symbol, {})
        fire, reason = should_fire(struct, prev)

        log.info(f"MarketStructure {symbol}: regime={struct['regime']} fire={fire} ({reason})")

        if fire:
            msg = format_telegram(struct)
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
    """Called by the scheduler. Polls BTC and ETH 1 minute apart."""
    poll_and_maybe_fire("BTCUSDT")
    time.sleep(60)
    poll_and_maybe_fire("ETHUSDT")


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
        msg = format_telegram(struct)
        send_text(msg)
    except Exception as e:
        log.exception(f"show_structure failed: {e}")
        send_text(f"📊 [MarketStructure] Error: {str(e)[:200]}")
