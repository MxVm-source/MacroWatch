# bot/modules/trigger.py
"""
trigger.py — combine market structure + CVD into a single gated verdict.

Encodes the discretionary gate rules so the bot names whether a level is a
LIVE trigger or a passed-but-valid NO-TAKE.

  at resistance + CVD rolling-over        -> FADE SHORT trigger LIVE
  at resistance + CVD rising / flat        -> absorption, NO-TAKE (named)
  at support    + CVD rising / flat (hold) -> LONG trigger LIVE
  at support    + CVD falling / rolling    -> sellers in control, NO-TAKE (named)
  not at any validated level               -> MID-RANGE, no trade

Breakout/breakdown (4H close beyond a level) is handled by close_break().

This module is adapted to consume market_structure_module.get_structure()'s
output directly — no separate dict-shape translation needed:
  s["spot"], s["res_levels"]=[(price,touches),...], s["sup_levels"]=[...],
  s["funding_now_pct"]  (NOTE: this is %/8h, NOT APR — converted internally)
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


def evaluate(structure: dict, symbol: str = "BTCUSDT", cvd_period: str = "15m", cvd=None):
    """
    structure: output of market_structure_module.get_structure().
    cvd: pre-fetched CVDResult (optional — fetched if not provided).
    """
    spot = structure["spot"]
    cvd  = cvd or get_cvd(symbol, period=cvd_period)

    res_levels = structure.get("res_levels", [])
    sup_levels = structure.get("sup_levels", [])

    near_res = _near(res_levels, 0.0, PROXIMITY_PCT, spot)
    near_sup = _near(sup_levels, -PROXIMITY_PCT, 0.0, spot)

    # funding_now_pct is %/8h; annualize to APR for the crowd check
    # (×3 settlements/day × 365 days)
    funding_pct_8h = structure.get("funding_now_pct", 0.0)
    funding_apr    = funding_pct_8h * 3 * 365

    crowd = ""
    if funding_apr > CROWD_APR:
        crowd = " | funding crowded LONG (fade-tilt)"
    elif funding_apr < -CROWD_APR:
        crowd = " | funding crowded SHORT (squeeze-tilt)"

    # Price/CVD divergence — surfaced regardless of state, including
    # MID_RANGE. This is the "flag early, before reaching a level" signal:
    # momentum can be quietly fading well before price gets anywhere near
    # a validated S/R zone.
    div_note = ""
    if cvd.divergence == "bearish":
        div_note = " | ⚠️ price rising but CVD not confirming (possible exhaustion)"
    elif cvd.divergence == "bullish":
        div_note = " | ⚠️ price falling but CVD not confirming (possible exhaustion)"

    state, side, level, reason = "MID_RANGE", None, None, "mid-range, no level in play"

    if cvd.direction == "unavailable":
        return {
            "state": "MID_RANGE", "side": None, "level": None,
            "reason": "CVD data unavailable — gate skipped",
            "cvd": cvd.as_dict(), "spot": spot,
        }

    if near_res is not None:
        level = near_res["price"]
        if cvd.direction == "rolling-over":
            state, side = "LIVE", "short"
            reason = "CVD rolling over into resistance = absorption confirmed"
        else:  # rising / flat / falling-but-still-below
            state, side = "NO_TAKE", "short"
            reason = f"CVD {cvd.direction} into resistance = absorption risk, no fade"

    elif near_sup is not None:
        level = near_sup["price"]
        if cvd.direction in ("rising", "flat"):
            state, side = "LIVE", "long"
            reason = f"CVD {cvd.direction}/holding at support = buyers defending"
        else:  # falling / rolling-over
            state, side = "NO_TAKE", "long"
            reason = f"CVD {cvd.direction} at support = sellers in control, no long"

    return {
        "state": state,            # LIVE | NO_TAKE | MID_RANGE
        "side": side,              # long | short | None
        "level": level,
        "reason": reason + crowd + div_note,
        "cvd": cvd.as_dict(),
        "spot": spot,
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
        head = f"{verdict['side'].upper()} TRIGGER LIVE @ {lvl_s}"
    elif verdict["state"] in ("BREAKOUT", "BREAKDOWN"):
        head = f"{verdict['state']} @ {lvl_s} ({verdict['side']})"
    else:  # NO_TAKE
        head = f"{lvl_s} {verdict['side']} = NO-TAKE"
    return f"{e} {head}\n   → {verdict['reason']}"
