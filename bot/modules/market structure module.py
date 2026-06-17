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
TOP_LEVELS      = int(os.getenv("MS_TOP_LEVELS", "4"))

STATE_PATH      = os.getenv("MS_STATE_PATH", "/var/data/market_structure_state.json")

# CVD trigger fire log — append-only JSONL, one line per LIVE/NO_TAKE verdict.
# Supports Phase 0 validation: compare bot verdicts against manual aggr 15m
# reads over the next 20-30 level-touches before promoting this to a real
# pre-filter. MID_RANGE verdicts are not logged (too frequent, not useful
# for this comparison).
CVD_LOG_PATH    = os.getenv("CVD_LOG_PATH", "/var/data/cvd_trigger_log.jsonl")

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
            if sym.startswith("_last_cvd_state_"):
                # CVD transition state — plain string (LIVE/NO_TAKE/MID_RANGE)
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

def fetch_klines(sym: str, interval: str = "4h", limit: int = NBARS) -> pd.DataFrame:
    """
    Fetch 4H OHLCV from Bitget USDT perp.

    CRITICAL: Bitget returns candles NEWEST-FIRST (descending timestamps).
      candles[0]  = most recent bar
      candles[-1] = oldest bar in this page

    Pagination: endTime = int(batch[-1][0]) - 1
    (matches atrb_multi_bot_v2.py fetch_daily_candles exactly)

    After collecting all pages, sort ascending before returning.
    """
    URL      = f"{BITGET_BASE}/api/v2/mix/market/candles"
    BATCH    = 200
    MS_4H    = 14_400_000
    all_rows = []
    end_ms   = None

    while len(all_rows) < limit:
        params = {
            "symbol":      sym,
            "granularity": interval,
            "limit":       str(min(BATCH, limit - len(all_rows))),
            "productType": PRODUCT_TYPE,
        }
        if end_ms:
            params["endTime"] = str(end_ms)

        r = requests.get(URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget candles err {data.get('code')}: {data.get('msg')}")
        batch = data.get("data") or []
        if not batch:
            break

        all_rows.extend(batch)

        # Newest-first: batch[-1] is the oldest bar in this page
        oldest_ts = int(batch[-1][0])
        end_ms    = oldest_ts - 1

        if len(batch) < BATCH:
            break
        time.sleep(0.2)

    if not all_rows:
        raise RuntimeError(f"fetch_klines: no data returned for {sym}")

    df = pd.DataFrame(all_rows, columns=["ot", "o", "h", "l", "c", "v", "qv"])
    df = df[["ot", "o", "h", "l", "c", "v"]].astype(
        {"o": float, "h": float, "l": float, "c": float, "v": float}
    )
    df["ot"] = df["ot"].astype(np.int64)
    df["t"]  = pd.to_datetime(df["ot"], unit="ms", utc=True)
    df = df.drop_duplicates("ot").sort_values("ot").reset_index(drop=True)

    n_bars = len(df)
    if n_bars >= 2:
        diffs  = df["ot"].diff().dropna()
        n_gaps = int((diffs != MS_4H).sum())
        log.info(
            f"fetch_klines {sym}: {n_bars} bars, "
            f"{df['t'].iloc[0].isoformat()} -> {df['t'].iloc[-1].isoformat()}, "
            f"gaps={n_gaps}"
        )
        if n_gaps > 0:
            bad = diffs[diffs != MS_4H]
            for idx, d in bad.items():
                log.warning(
                    f"fetch_klines {sym}: gap at bar {idx} "
                    f"({df['t'].iloc[idx-1].isoformat()} -> "
                    f"{df['t'].iloc[idx].isoformat()}, diff={d}ms)"
                )

    return df


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


def open_interest_data(sym: str, limit: int = 120):
    """
    Current OI from Bitget. Handles both single-dict and list response shapes.
    Builds a rolling history in STATE since Bitget doesn't expose OI history.
    """
    try:
        resp = requests.get(BITGET_OI,
                            params={"symbol": sym, "productType": PRODUCT_TYPE},
                            timeout=15).json()
        data = resp.get("data") or {}

        # data can be a single dict {"symbol":..,"amount":..} or a list of dicts
        if isinstance(data, dict):
            oi_now = float(data.get("amount") or data.get("size") or data.get("openInterest") or 0)
        elif isinstance(data, list) and data:
            row = next((r for r in data if isinstance(r, dict) and r.get("symbol") == sym), data[0])
            oi_now = float(row.get("amount") or row.get("size") or row.get("openInterest") or 0)
        else:
            oi_now = 0.0

        # Rolling OI history kept in persistent STATE (bar-by-bar, capped at limit).
        # Skip recording oi_now==0 — real BTC OI is never 0, so a 0.0 reading
        # means Bitget returned empty/malformed data this round. Recording it
        # would poison the trend ratio (X/0 -> inf/nan) for up to `limit` bars.
        hist_key = f"_oi_hist_{sym}"
        oi_hist  = STATE.get(hist_key, [])
        if not isinstance(oi_hist, list):
            oi_hist = []
        if oi_now > 0:
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
    Are we AT a tradeable level (within proximity AND validated by touch
    count), or mid-range no-trade?

    Thresholds are imported from trigger.py so the headline verdict (✅/⏳)
    and the CVD gate verdict (LIVE/NO_TAKE/MID_RANGE) always agree on
    whether spot is "at a level" — a ×1 level within 2% but failing
    trigger.py's MIN_TOUCHES would otherwise produce a confusing
    "✅ At support..." + "➖ TRIGGER: none, mid-range" combination.

    Returns (emoji_status, text, near_price_or_None).
    near_price marks which level (if any) the spot is currently sitting at,
    so format_telegram can flag it with "← HERE" without re-deriving it.
    """
    try:
        from bot.modules.trigger import PROXIMITY_PCT as GATE_PROXIMITY_PCT, MIN_TOUCHES as GATE_MIN_TOUCHES
        proximity = GATE_PROXIMITY_PCT / 100.0   # trigger.py uses percent units (0.6 = 0.6%)
        min_touches = GATE_MIN_TOUCHES
    except Exception:
        # Fallback if trigger.py is unavailable — keep prior behaviour
        proximity = PROXIMITY_PCT
        min_touches = 1

    near_res = None
    near_sup = None
    for p, n in res_levels:
        if n >= min_touches and abs(p - spot) / spot <= proximity:
            near_res = (p, n)
            break
    for p, n in sup_levels:
        if n >= min_touches and abs(p - spot) / spot <= proximity:
            near_sup = (p, n)
            break

    if near_res:
        return "✅", f"At resistance {near_res[0]:,.0f} (×{near_res[1]}) — fade or breakout watch", near_res[0]
    if near_sup:
        return "✅", f"At support {near_sup[0]:,.0f} (×{near_sup[1]}) — bounce or breakdown watch", near_sup[0]

    # Mid-range — compute distance to closest level (any touch count, for
    # the "next level" hint — this part doesn't need to match the gate)
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
    oi_samples = len(oh)
    if oi_samples >= 2 and oh["sumOpenInterest"].iloc[0] > 0:
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

def _log_trigger_fire(verdict: dict, symbol: str):
    """
    Append a JSONL entry for LIVE/NO_TAKE verdicts only — supports Phase 0
    validation (compare against manual aggr 15m reads). Isolated try/except,
    a logging failure must never affect the broadcast itself.
    """
    if verdict["state"] not in ("LIVE", "NO_TAKE"):
        return
    try:
        os.makedirs(os.path.dirname(CVD_LOG_PATH), exist_ok=True)
        entry = {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "symbol":        symbol,
            "spot":          verdict.get("spot"),
            "level":         verdict.get("level"),
            "side":          verdict.get("side"),
            "state":         verdict.get("state"),
            "cvd_direction": verdict["cvd"].get("direction"),
            "divergence":    verdict["cvd"].get("divergence"),
            "reason":        verdict.get("reason"),
        }
        with open(CVD_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"CVD trigger log write failed: {e}")


def _check_cvd_transition(verdict: dict, symbol: str):
    """
    Detect LIVE <-> NO_TAKE state transitions and fire a dedicated alert.

    CADENCE: called ONLY from poll_and_maybe_fire (the 4H cron job).
    Never called from show_structure (/structure command) — that's a read-only
    view, not a state-machine tick. One evaluation per 4H candle, max.
    This prevents LIVE->NO_TAKE->LIVE chop alerts in sideways markets.

    PHASE-0 LANGUAGE: bot CVD is an unvalidated Bitget-perp proxy.
    Alerts say "check aggr" not "act" until 20-30 trades of agreement
    are logged in cvd_trigger_log.jsonl and the proxy is promoted.

    Transitions that fire:
      NO_TAKE  -> LIVE      check aggr — CVD now confirms
      LIVE     -> NO_TAKE   check aggr — CVD withdrawn
      LIVE     -> MID_RANGE level left while signal was live
      NO_TAKE  -> MID_RANGE level no longer in play
    """
    state_key  = f"_last_cvd_state_{symbol}"
    prev_state = STATE.get(state_key)
    new_state  = verdict["state"]

    STATE[state_key] = new_state
    _save_state()  # persist immediately — must survive redeploys

    actionable = {
        ("NO_TAKE",  "LIVE"),
        ("LIVE",     "NO_TAKE"),
        ("LIVE",     "MID_RANGE"),
        ("NO_TAKE",  "MID_RANGE"),
    }

    if prev_state is None or (prev_state, new_state) not in actionable:
        return

    spot   = verdict.get("spot", 0)
    level  = verdict.get("level")
    side   = verdict.get("side") or ""
    reason = verdict.get("reason", "")
    lvl_s  = f"@ `{level:,.0f}`" if level else ""
    AGGR_URL = "https://aggr.trade/6zn3"

    if new_state == "LIVE":
        icon = "🟢"
        head = f"CVD → LIVE — {side.upper()} {lvl_s}"
        action = f"→ [Check aggr 15m CVD]({AGGR_URL}) before acting (Phase-0: proxy unvalidated)"
    elif new_state == "NO_TAKE":
        icon = "⚪"
        head = f"CVD → NO-TAKE — {side.upper()} {lvl_s}"
        action = f"→ [Check aggr 15m CVD]({AGGR_URL}) — bot says absorption, confirm or override"
    else:  # MID_RANGE
        icon = "➖"
        head = "CVD → MID-RANGE — level left"
        action = "→ Level no longer in play"

    msg = (
        f"{icon} *{symbol} — {head}*\n"
        f"`${spot:,.0f}` | `{prev_state}` → `{new_state}`\n"
        f"{reason}\n"
        f"{action}"
    )

    try:
        send_text(msg)
        log.info(f"CVD transition alert: {symbol} {prev_state} -> {new_state}")
    except Exception as e:
        log.warning(f"CVD transition alert send failed: {e}")


def _get_trigger_line(struct: dict, symbol: str, state_update: bool = True) -> str | None:
    """
    Run the CVD gate and return its formatted line for the broadcast message.

    state_update=True  (default, 4H cron path): also logs the verdict and
                       runs transition detection. One state tick per candle.
    state_update=False (/structure command path): compute and display only —
                       never advances the state machine or fires transition
                       alerts. The command is a read-only view, not a tick.
    """
    try:
        from bot.modules.trigger import evaluate, format_trigger_line
        verdict = evaluate(struct, symbol=symbol)
        if verdict["cvd"].get("direction") == "unavailable":
            return None
        if state_update:
            _log_trigger_fire(verdict, symbol)
            _check_cvd_transition(verdict, symbol)
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
        else:
            # Even when the full structure broadcast is suppressed, still run
            # the CVD gate for transition detection — a LIVE→NO_TAKE flip is
            # important to catch regardless of whether anything else changed.
            _get_trigger_line(struct, symbol)

    except Exception as e:
        log.exception(f"MarketStructure poll failed for {symbol}: {e}")


def poll_all():
    """Called by the scheduler. BTC only — ETH remains available on-demand
    via /structure ETH but is not auto-broadcast."""
    poll_and_maybe_fire("BTCUSDT")


# ─── Command entrypoint ──────────────────────────────────────────────────────

def show_cvd_log(n: int = 10):
    """Handler for /cvd_log [N] — dump the last N trigger fire entries."""
    try:
        if not os.path.exists(CVD_LOG_PATH):
            send_text("📊 CVD log is empty — no LIVE/NO_TAKE verdicts recorded yet.")
            return
        with open(CVD_LOG_PATH) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            send_text("📊 CVD log is empty.")
            return
        entries = [json.loads(l) for l in lines[-n:]]
        parts = [f"📊 *CVD Trigger Log* — last {len(entries)} entries\n"]
        for e in reversed(entries):
            ts  = e.get("ts", "")[:16].replace("T", " ")
            sym = e.get("symbol", "")
            st  = e.get("state", "")
            sd  = e.get("side", "") or ""
            lvl = e.get("level")
            cvd = e.get("cvd_direction", "")
            div = e.get("divergence", "none")
            icon = "🟢" if st == "LIVE" else "⚪"
            lvl_s = f"@ `{lvl:,.0f}`" if lvl else ""
            div_s = f" | div: {div}" if div and div != "none" else ""
            parts.append(
                f"{icon} `{ts}` — {sym}\n"
                f"  {st} {sd.upper()} {lvl_s}\n"
                f"  CVD: {cvd}{div_s}"
            )
        send_text("\n\n".join(parts))
    except Exception as e:
        send_text(f"📊 [CVD Log] Error: {str(e)[:200]}")


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
        trigger_line = _get_trigger_line(struct, sym, state_update=False)
        msg = format_telegram(struct, trigger_line=trigger_line)
        send_text(msg)
    except Exception as e:
        log.exception(f"show_structure failed: {e}")
        send_text(f"📊 [MarketStructure] Error: {str(e)[:200]}")
