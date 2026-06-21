"""
structural_stop.py
==================
Structural stop-loss selection for level-based entries.

Replaces the fixed-percentage SL in the proposal engine. A level-based
short/long must be stopped BEYOND the next *significant* structural level,
not at an arbitrary % distance that lands inside the chop and gets wicked.

Drop-in: import into trigger.py and call structural_stop() when building a
proposal, then recompute R:R with honest_rr() so the displayed numbers are
measured against the real stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- tunables ----------------------------------------------------------------
STOP_MIN_TOUCHES = 3       # ignore weak (x1/x2) levels when picking the wall
STOP_BUFFER_PCT  = 0.0018  # 0.18% beyond the protective level (wick clearance)
MAX_RISK_PCT     = 0.22    # flag if SL implies > this fraction of capital at risk
TP1_MIN_RR       = 1.0     # flag if nearest TP is sub-1R against the structural stop
TICK             = 0.1     # Bitget price increment for snapping


def _snap(p: float) -> float:
    return round(round(p / TICK) * TICK, 1)


@dataclass
class StopResult:
    sl: float
    protective_level: float | None
    protective_touches: int | None
    risk_pct: float          # fraction of capital at risk = sl_dist_pct * leverage
    sl_dist_pct: float
    mode: str                # "structural" or "fallback_atr"
    warnings: list[str] = field(default_factory=list)


def structural_stop(
    entry: float,
    side: str,                        # "short" or "long"
    levels: list[tuple[float, int]],  # full S/R set: [(price, touches), ...]
    leverage: float,
    *,
    atr: float | None = None,         # fallback only if no qualifying level exists
    min_touches: int = STOP_MIN_TOUCHES,
    buffer_pct: float = STOP_BUFFER_PCT,
) -> StopResult:
    """
    SHORT: SL = (nearest level ABOVE entry with >= min_touches) + buffer.
    LONG : SL = (nearest level BELOW entry with >= min_touches) - buffer.

    Weak intervening levels (x1/x2) are intentionally skipped: a wick through a
    thin level is noise; the stop belongs beyond the next real wall, or it is a
    4H-close trigger (see note at bottom of file).
    """
    side = side.lower()
    warnings: list[str] = []

    if side == "short":
        above = sorted(
            [(p, t) for (p, t) in levels if p > entry and t >= min_touches],
            key=lambda x: x[0],
        )
        if above:
            lvl_price, lvl_touches = above[0]
            sl = lvl_price * (1 + buffer_pct)
            mode = "structural"
        else:
            lvl_price = lvl_touches = None
            if atr is None:
                raise ValueError("no qualifying level above entry and no ATR fallback")
            sl = entry + 1.5 * atr
            mode = "fallback_atr"
            warnings.append(f"no x{min_touches}+ level above entry; used 1.5*ATR fallback")
        sl_dist = sl - entry

    elif side == "long":
        below = sorted(
            [(p, t) for (p, t) in levels if p < entry and t >= min_touches],
            key=lambda x: -x[0],
        )
        if below:
            lvl_price, lvl_touches = below[0]
            sl = lvl_price * (1 - buffer_pct)
            mode = "structural"
        else:
            lvl_price = lvl_touches = None
            if atr is None:
                raise ValueError("no qualifying level below entry and no ATR fallback")
            sl = entry - 1.5 * atr
            mode = "fallback_atr"
            warnings.append(f"no x{min_touches}+ level below entry; used 1.5*ATR fallback")
        sl_dist = entry - sl

    else:
        raise ValueError(f"side must be 'short' or 'long', got {side!r}")

    sl = _snap(sl)
    sl_dist = abs(sl - entry)
    sl_dist_pct = sl_dist / entry
    risk_pct = sl_dist_pct * leverage

    if risk_pct > MAX_RISK_PCT:
        warnings.append(f"risk {risk_pct:.1%} > guardrail {MAX_RISK_PCT:.0%}: cut size or skip")

    return StopResult(
        sl=sl,
        protective_level=lvl_price,
        protective_touches=lvl_touches,
        risk_pct=risk_pct,
        sl_dist_pct=sl_dist_pct,
        mode=mode,
        warnings=warnings,
    )


def honest_rr(entry: float, sl: float, tps: list[float], side: str) -> list[float]:
    """R:R per TP measured against the structural stop (not a tight % stop)."""
    risk = abs(entry - sl)
    if risk == 0:
        raise ValueError("zero stop distance")
    sign = 1.0 if side.lower() == "short" else -1.0
    return [round(sign * (entry - tp) / risk, 2) for tp in tps]


def grade_tps(rr: list[float], min_rr: float = TP1_MIN_RR) -> list[str]:
    """Flag a nearest TP that is sub-min_rr against the structural stop."""
    w = []
    if rr and rr[0] < min_rr:
        w.append(f"TP1 is {rr[0]:.2f}R against the structural stop (< {min_rr:.1f}R): "
                 f"move TP1 deeper or accept it is a scalp leg, not a 30% A-grade leg")
    return w


# --- self-test against the live 2026-06-21 proposal --------------------------
if __name__ == "__main__":
    entry = 64294.94
    levels = [
        (64232.8, 5), (65081.0, 5), (65718.5, 10), (66588.0, 7),   # resistance
        (63650.0, 1), (62979.5, 2), (62401.7, 3), (61344.8, 2),    # support
    ]
    tps = [63651.70, 63060.65, 62322.45]

    res = structural_stop(entry, "short", levels, leverage=10)
    rr = honest_rr(entry, res.sl, tps, "short")

    print(f"entry           {entry:,.2f}")
    print(f"protective lvl  {res.protective_level:,.1f} (x{res.protective_touches})")
    print(f"SL (structural) {res.sl:,.1f}  [{res.sl_dist_pct:.2%} -> risk {res.risk_pct:.1%}]")
    print(f"bot SL (fixed)  64,616.41  [0.50% -> risk 5.0%]   <-- inflates R:R")
    print(f"honest R:R      TP1 {rr[0]}  TP2 {rr[1]}  TP3 {rr[2]}")
    print(f"bot showed      TP1 2.00  TP2 3.84  TP3 6.14")
    for wmsg in res.warnings + grade_tps(rr):
        print(f"  WARN: {wmsg}")
