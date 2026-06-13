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
"""

import logging
from dataclasses import dataclass, asdict

import requests

log = logging.getLogger("cvd")

BITGET_BASE       = "https://api.bitget.com"
BITGET_TAKER_BUYSELL = f"{BITGET_BASE}/api/v2/mix/market/taker-buy-sell"
PRODUCT_TYPE      = "USDT-FUTURES"


@dataclass
class CVDResult:
    cvd_now: float        # running cumulative delta over the lookback window
    slope_recent: float   # CVD change over the recent window (signed)
    direction: str        # rising | rolling_over | falling | flat | unavailable
    price_slope: float    # close change over the recent window (signed) — N/A here, kept for API parity
    divergence: str       # bearish | bullish | none
    last_price: float      # N/A here (0.0) — kept for API parity with trigger.py

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


def _unavailable() -> CVDResult:
    return CVDResult(
        cvd_now=0.0, slope_recent=0.0, direction="unavailable",
        price_slope=0.0, divergence="none", last_price=0.0,
    )


def get_cvd(symbol: str = "BTCUSDT", period: str = "15m", lookback: int = 96,
            recent: int = 5, flat_eps_frac: float = 0.05,
            _rows=None) -> CVDResult:
    """
    lookback : periods summed for the running CVD (96 x 15m = 24h)
    recent   : periods used for the slope read (5 x 15m = ~75 min)
    flat_eps_frac : |recent CVD slope| under this fraction of recent
                    absolute flow is treated as 'flat'
    _rows    : inject buy/sell rows for testing (skips the network call)
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

    # Price slope / divergence — not computed here since we don't fetch
    # closes in this endpoint. trigger.py only uses `direction`, so this
    # is fine; left as 0.0/"none" for API parity.
    return CVDResult(
        cvd_now=round(cvd_now, 2),
        slope_recent=round(slope_recent, 2),
        direction=direction,
        price_slope=0.0,
        divergence="none",
        last_price=0.0,
    )
