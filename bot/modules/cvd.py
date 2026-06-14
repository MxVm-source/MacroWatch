# bot/modules/cvd.py
"""
cvd.py — Cumulative Volume Delta from Bitget USDT-M perp taker buy/sell volume.

Ported from the original Binance-based design (HTTP 451 geo-blocks Binance
from Render — same issue market_structure_module.py already solved by
switching to Bitget). Bitget's /v2/mix/market/taker-buy-sell endpoint gives
the buy/sell split DIRECTLY (no 2*tb-v reconstruction needed), which is
actually cleaner than the Binance approach.

Candle-close granularity (not live tape) — enough for "is CVD confirming or
diverging at this level," which is the Tier-1 gate need. Treat it as the gate
signal, not a tick-level replica of aggr.trade.

Gate vocabulary (direction):
  rising        - aggressive buyers in control over the recent window
  rolling_over  - was rising, now turning down (fade-confirmation signal)
  falling       - aggressive sellers in control
  flat          - no clear pressure
  unavailable   - data fetch failed; caller should omit the trigger line

Divergence (price vs CVD, computed from a separate closes fetch):
  bearish - price rising but CVD flat/falling = buyers not confirming
            (possible absorption/exhaustion ahead of a high)
  bullish - price falling but CVD flat/rising = sellers not confirming
            (possible exhaustion ahead of a low)
  none    - price and CVD agree, or closes fetch failed (degrades silently —
            `direction` above remains the primary, always-available signal)
"""

import logging
from dataclasses import dataclass, asdict

import requests

log = logging.getLogger("cvd")

BITGET_BASE       = "https://api.bitget.com"
BITGET_TAKER_BUYSELL = f"{BITGET_BASE}/api/v2/mix/market/taker-buy-sell"
BITGET_CANDLES    = f"{BITGET_BASE}/api/v2/mix/market/candles"
PRODUCT_TYPE      = "USDT-FUTURES"


@dataclass
class CVDResult:
    cvd_now: float        # running cumulative delta over the lookback window
    slope_recent: float   # CVD change over the recent window (signed)
    direction: str        # rising | rolling_over | falling | flat | unavailable
    price_slope: float    # close change over the recent window (signed)
    divergence: str       # bearish | bullish | none
    last_price: float      # most recent close (0.0 if closes fetch failed)

    def as_dict(self):
        return asdict(self)


def _fetch_taker_buysell(symbol: str = "BTCUSDT", period: str = "15m", limit: int = 100):
    """
    Fetch buy/sell volume split from Bitget. Returns list of dicts:
      [{"buyVolume": float, "sellVolume": float, "ts": int}, ...]
    oldest-first.
    """
    r = requests.get(
        BITGET_TAKER_BUYSELL,
        params={"symbol": symbol, "period": period, "limit": str(limit),
                "productType": PRODUCT_TYPE},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget taker-buy-sell err {data.get('code')}: {data.get('msg')}")
    rows = data.get("data") or []
    out = []
    for row in rows:
        out.append({
            "buyVolume":  float(row.get("buyVolume", 0) or 0),
            "sellVolume": float(row.get("sellVolume", 0) or 0),
            "ts":         int(row.get("ts", 0) or 0),
        })
    # Bitget convention varies; ensure ascending by ts
    out.sort(key=lambda x: x["ts"])
    return out


def _fetch_closes(symbol: str = "BTCUSDT", period: str = "15m", limit: int = 100):
    """
    Fetch recent closes from Bitget candles (same period as the CVD read),
    used only for price/CVD divergence detection. At 15m/96 bars this fits
    in a single request (Bitget v2 cap is 200/request) — no pagination.
    Returns ascending-by-time list of floats.
    """
    r = requests.get(
        BITGET_CANDLES,
        params={"symbol": symbol, "granularity": period, "limit": str(limit),
                "productType": PRODUCT_TYPE},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "00000":
        raise RuntimeError(f"Bitget candles err {data.get('code')}: {data.get('msg')}")
    candles = data.get("data") or []
    candles.sort(key=lambda c: int(c[0]))  # ascending by timestamp
    return [float(c[4]) for c in candles]


def _unavailable() -> CVDResult:
    return CVDResult(
        cvd_now=0.0, slope_recent=0.0, direction="unavailable",
        price_slope=0.0, divergence="none", last_price=0.0,
    )


def get_cvd(symbol: str = "BTCUSDT", period: str = "15m", lookback: int = 96,
            recent: int = 5, flat_eps_frac: float = 0.05,
            _rows=None, _closes=None) -> CVDResult:
    """
    lookback : periods summed for the running CVD (96 x 15m = 24h)
    recent   : periods used for the slope read (5 x 15m = ~75 min)
    flat_eps_frac : |recent CVD slope| under this fraction of recent
                    absolute flow is treated as 'flat'
    _rows    : inject buy/sell rows for testing (skips the network call)
    _closes  : inject closes for testing (skips the candles network call)
    """
    try:
        rows = _rows if _rows is not None else _fetch_taker_buysell(symbol, period, lookback)
    except Exception as e:
        log.warning(f"CVD fetch failed for {symbol}: {e}")
        return _unavailable()

    if len(rows) < 2 * recent + 1:
        log.warning(f"CVD: insufficient data for {symbol} "
                     f"(got {len(rows)}, need >= {2*recent+1})")
        return _unavailable()

    deltas = [r["buyVolume"] - r["sellVolume"] for r in rows]

    # running CVD series
    cvd_series, acc = [], 0.0
    for d in deltas:
        acc += d
        cvd_series.append(acc)

    cvd_now      = cvd_series[-1]
    slope_recent = cvd_series[-1] - cvd_series[-1 - recent]

    # flatness threshold scaled to how much flow happened recently
    recent_abs = sum(abs(x) for x in deltas[-recent:]) or 1.0
    flat_eps   = recent_abs * flat_eps_frac

    if slope_recent > flat_eps:
        direction = "rising"
    elif slope_recent < -flat_eps:
        prev_slope = cvd_series[-1 - recent] - cvd_series[-1 - 2 * recent]
        direction = "rolling_over" if prev_slope > flat_eps else "falling"
    else:
        direction = "flat"

    # Price/CVD divergence — needs closes, fetched separately (taker-buy-sell
    # doesn't return price). A closes-fetch failure must NOT take down the
    # whole CVD read: direction (above) is the primary signal and degrades
    # gracefully to price_slope=0.0 / divergence="none" if this fails.
    price_slope = 0.0
    last_price  = 0.0
    divergence  = "none"
    try:
        closes = _closes if _closes is not None else _fetch_closes(symbol, period, lookback)
        n = min(len(closes), len(cvd_series))
        if n >= recent + 1:
            closes = closes[-n:]
            price_slope = closes[-1] - closes[-1 - recent]
            last_price  = closes[-1]

            if price_slope > 0 and slope_recent <= flat_eps:
                divergence = "bearish"   # price up, buyers not confirming = absorption
            elif price_slope < 0 and slope_recent >= -flat_eps:
                divergence = "bullish"   # price down, sellers not confirming
    except Exception as e:
        log.warning(f"CVD: closes fetch failed for {symbol} (divergence skipped): {e}")

    return CVDResult(
        cvd_now=round(cvd_now, 2),
        slope_recent=round(slope_recent, 2),
        direction=direction,
        price_slope=round(price_slope, 2),
        divergence=divergence,
        last_price=last_price,
    )
