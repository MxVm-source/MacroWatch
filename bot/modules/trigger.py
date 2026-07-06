# bot/modules/trigger.py
"""
trigger.py — combine market structure + CVD into a single gated verdict.

Encodes the discretionary gate rules so the bot names whether a level is a
LIVE trigger or a passed-but-valid NO-TAKE.

  at resistance + CVD rolling_over        -> FADE SHORT trigger LIVE
  at resistance + CVD rising / flat        -> absorption, NO-TAKE (named)
  at support    + CVD rising / flat (hold) -> LONG trigger LIVE
  at support    + CVD falling / rolling    -> sellers in control, NO-TAKE (named)
  not at any validated level               -> MID-RANGE, no trade

REGIME LAYER (Case-1 discipline refinement):
  A LIVE verdict is further graded by trend context, WITHOUT changing the
  LIVE/NO_TAKE/MID_RANGE state (so every existing consumer is unaffected):
    with-trend into a wall (short in a downtrend / long in an uptrend)
        -> entry_mode = "intrabar"  (resting limit at level + CVD gate, no close-wait)
    counter-trend, or first touch after a flush
        -> entry_mode = "await_4h"  (needs a 4H hold/reclaim before entering)
  Two new keys are ADDED to the verdict: with_trend (bool|None), entry_mode
  ("intrabar"|"await_4h"|None). The intrabar scanner reads entry_mode to decide
  whether a GO may fire sub-candle; the 4H path ignores it and behaves as before.

Breakout/breakdown (4H close beyond a level) is handled by close_break().

Consumes market_structure_module.get_structure() output directly:
  s["spot"], s["res_levels"]=[(price,touches),...], s["sup_levels"]=[...],
  s["funding_now_pct"] (%/8h, converted internally), s["regime"]
  (BULL-EXP/BULL-RANGE/BEAR-EXP/BEAR-RANGE/CHOP/UNKNOWN).
"""

import logging
from bot.modules.cvd import get_cvd

log = logging.getLogger("trigger")

PROXIMITY_PCT = 0.6   # within this % of a level counts as "at" it
MIN_TOUCHES   = 2     # a validated level needs >= this many touches
CROWD_APR     = 8.0   # |funding APR| above this = crowded-side flag


def _near(levels, lo_pct, hi_pct, spot):
    """
    levels: [(price, touches), ...]
    Returns the nearest validated level whose distance from spot (as a
    signed %) falls within [lo_pct, hi_pct].
    """
    for price, touches in levels:
        if touches < MIN_TOUCHES:
            continue
        dist_pct = (price - spot) / spot * 100
        if lo_pct <= dist_pct <= hi_pct:
            return {"price": price, "touches": touches, "dist_pct": dist_pct}
    return None


def _regime_bias(regime: str) -> str:
    """BEAR-* -> 'bear', BULL-* -> 'bull', CHOP/UNKNOWN/'' -> 'neutral'."""
    r = (regime or "").upper()
    if r.startswith("BEAR"):
        return "bear"
    if r.startswith("BULL"):
        return "bull"
    return "neutral"


def _grade_entry(side: str, bias: str, fresh_flush: bool):
    """
    Given a LIVE side and the regime bias, decide intrabar vs await-4h.
    Returns (with_trend: bool, entry_mode: str, note: str).
      with-trend  -> short in bear / long in bull          -> intrabar GO
      neutral     -> CHOP/UNKNOWN (range-fade both edges)  -> intrabar GO
      counter     -> short in bull / long in bear          -> await 4H
      fresh_flush -> always await 4H (knife catch)
    """
    if side == "short":
        with_trend = (bias == "bear")
        counter    = (bias == "bull")
    else:  # long
        with_trend = (bias == "bull")
        counter    = (bias == "bear")

    if fresh_flush:
        return with_trend, "await_4h", "first touch after flush — await 4H hold/reclaim"
    if counter:
        return with_trend, "await_4h", f"counter-trend ({bias} regime) — await 4H hold/reclaim"
    tag = "with-trend" if with_trend else "range-fade"
    return with_trend, "intrabar", f"{tag}, limit OK"


def evaluate(structure: dict, symbol: str = "BTCUSDT", cvd_period: str = "15m",
             cvd=None, fresh_flush: bool = False):
    """
    structure: output of market_structure_module.get_structure().
    cvd: pre-fetched CVDResult (optional — fetched if not provided).
    fresh_flush: caller may flag a first-touch-after-flush to force await_4h.
    """
    spot = structure["spot"]
    cvd  = cvd or get_cvd(symbol, period=cvd_period)
    # Normalize so 'rolling-over' / 'rolling_over' both match cvd.py's output.
    cvd_dir = (cvd.direction or "").replace("_", "-")

    res_levels = structure.get("res_levels", [])
    sup_levels = structure.get("sup_levels", [])
    regime     = structure.get("regime", "")
    bias       = _regime_bias(regime)

    near_res = _near(res_levels, 0.0, PROXIMITY_PCT, spot)
    near_sup = _near(sup_levels, -PROXIMITY_PCT, 0.0, spot)

    # funding_now_pct is %/8h; annualize to APR for the crowd check
    funding_pct_8h = structure.get("funding_now_pct", 0.0)
    funding_apr    = funding_pct_8h * 3 * 365

    crowd = ""
    if funding_apr > CROWD_APR:
        crowd = " | funding crowded LONG (fade-tilt)"
    elif funding_apr < -CROWD_APR:
        crowd = " | funding crowded SHORT (squeeze-tilt)"

    # Price/CVD divergence — surfaced regardless of state.
    div_note = ""
    if cvd.divergence == "bearish":
        div_note = " | ⚠️ price rising but CVD not confirming (possible exhaustion)"
    elif cvd.divergence == "bullish":
        div_note = " | ⚠️ price falling but CVD not confirming (possible exhaustion)"

    state, side, level, reason = "MID_RANGE", None, None, "mid-range, no level in play"
    with_trend, entry_mode = None, None

    if cvd.direction == "unavailable":
        return {
            "state": "MID_RANGE", "side": None, "level": None,
            "reason": "CVD data unavailable — gate skipped",
            "cvd": cvd.as_dict(), "spot": spot,
            "regime": regime, "with_trend": None, "entry_mode": None,
        }

    if near_res is not None:
        level = near_res["price"]
        if cvd_dir == "rolling-over":
            state, side = "LIVE", "short"
            with_trend, entry_mode, note = _grade_entry(side, bias, fresh_flush)
            reason = f"CVD rolling over into resistance = absorption confirmed | {note}"
        else:  # rising / flat / falling-but-not-yet-rolled-over
            state, side = "NO_TAKE", "short"
            if cvd_dir == "falling":
                reason = ("CVD falling but hasn't rolled over yet — waiting for the turn, "
                           "not chasing an already-extended move")
            else:
                reason = f"CVD {cvd.direction} into resistance = absorption risk, no fade"

    elif near_sup is not None:
        level = near_sup["price"]
        if cvd_dir in ("rising", "flat"):
            state, side = "LIVE", "long"
            with_trend, entry_mode, note = _grade_entry(side, bias, fresh_flush)
            reason = f"CVD {cvd.direction}/holding at support = buyers defending | {note}"
        else:  # falling / rolling_over
            state, side = "NO_TAKE", "long"
            reason = f"CVD {cvd.direction} at support = sellers in control, no long"

    return {
        "state": state,            # LIVE | NO_TAKE | MID_RANGE  (unchanged)
        "side": side,              # long | short | None
        "level": level,
        "reason": reason + crowd + div_note,
        "cvd": cvd.as_dict(),
        "spot": spot,
        "regime": regime,          # NEW
        "with_trend": with_trend,  # NEW: True/False/None
        "entry_mode": entry_mode,  # NEW: "intrabar" | "await_4h" | None
    }


def close_break(close_price, ceiling=None, floor=None, cvd=None,
                symbol="BTCUSDT", cvd_period="15m"):
    """
    Call once on the confirmed 4H close. ceiling/floor = the range edges
    being watched (e.g. nearest resistance / support from get_structure()).
    Returns a breakout/breakdown verdict or None.
    """
    cvd = cvd or get_cvd(symbol, period=cvd_period)
    if cvd.direction == "unavailable":
        return None
    if ceiling and close_price > ceiling:
        return {
            "state": "BREAKOUT", "side": "long", "level": ceiling,
            "reason": f"4H close {close_price:.0f} > {ceiling:.0f}"
                      f" | CVD {cvd.direction}",
            "cvd": cvd.as_dict(),
        }
    if floor and close_price < floor:
        return {
            "state": "BREAKDOWN", "side": "short", "level": floor,
            "reason": f"4H close {close_price:.0f} < {floor:.0f}"
                      f" | CVD {cvd.direction}",
            "cvd": cvd.as_dict(),
        }
    return None


# ---- Telegram formatting -------------------------------------------------

_EMOJI = {"LIVE": "🟢", "NO_TAKE": "⚪", "MID_RANGE": "➖",
          "BREAKOUT": "🚀", "BREAKDOWN": "🔻"}


def format_trigger_line(verdict: dict) -> str:
    """One-line trigger verdict to append under the structure broadcast."""
    e = _EMOJI.get(verdict["state"], "")
    if verdict["state"] == "MID_RANGE":
        return f"{e} TRIGGER: none — {verdict['reason']}"
    lvl   = verdict.get("level")
    lvl_s = f"{lvl:,.0f}" if lvl else "—"
    if verdict["state"] == "LIVE":
        if verdict.get("entry_mode") == "await_4h":
            e    = "⏳"
            head = f"{verdict['side'].upper()} {lvl_s} — AWAIT 4H (counter-trend/flush)"
        else:
            head = f"{verdict['side'].upper()} TRIGGER LIVE @ {lvl_s} (with-trend)"
    elif verdict["state"] in ("BREAKOUT", "BREAKDOWN"):
        head = f"{verdict['state']} @ {lvl_s} ({verdict['side']})"
    else:  # NO_TAKE
        head = f"{lvl_s} {verdict['side']} = NO-TAKE"
    return f"{e} {head}\n   → {verdict['reason']}"
