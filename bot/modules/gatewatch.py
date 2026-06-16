# bot/modules/gatewatch.py
"""
gatewatch.py — /gate : on-demand discretionary-gate read.

Runs trigger.evaluate() against the live market structure + CVD proxy, posts the
verdict (LIVE / NO_TAKE / MID_RANGE), and appends the read to the Phase-0 JSONL
log so proxy-vs-aggr agreement can be measured over a real sample.

PROXY ONLY. This does NOT replace the aggr.trade read at entry until Phase-0
validation clears (~20-30 trades of logged agreement). The card says so, every
time, on purpose.
"""

import os
import json
import logging
from datetime import datetime, timezone

from bot.utils import send_text
from bot.modules import market_structure_module as msm
from bot.modules import trigger as trig

log = logging.getLogger("gatewatch")

# Point this at the SAME file /cvd_log writes to (see note in chat). Move off
# /tmp onto a persistent disk for Phase-0 integrity.
CVD_LOG_PATH = os.getenv("CVD_LOG_PATH", "/tmp/cvd_log.jsonl")


def _get_structure(symbol: str):
    """get_structure may or may not take a symbol arg — handle both."""
    try:
        return msm.get_structure(symbol)
    except TypeError:
        return msm.get_structure()


def _nearest(levels, above: bool, spot: float):
    """levels: [(price, touches), ...]. Nearest level above/below spot."""
    cands = [(p, t) for (p, t) in levels if (p > spot if above else p < spot)]
    if not cands:
        return None
    return min(cands, key=lambda x: abs(x[0] - spot))


def _log(record: dict):
    try:
        with open(CVD_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"gate log failed: {e}")


def run_gate(symbol: str = "BTCUSDT"):
    symbol = (symbol or "BTCUSDT").upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    structure = _get_structure(symbol)
    spot      = structure["spot"]
    verdict   = trig.evaluate(structure, symbol=symbol)
    cvd       = verdict["cvd"]

    res = _nearest(structure.get("res_levels", []), True,  spot)
    sup = _nearest(structure.get("sup_levels", []), False, spot)
    funding_8h  = structure.get("funding_now_pct", 0.0)
    funding_apr = funding_8h * 3 * 365
    ts = datetime.now(timezone.utc).isoformat()

    # ── card ──
    lines = [
        f"🎯 *GATE — {symbol}*",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Spot: {spot:,.2f}",
    ]
    if res:
        lines.append(f"Res: {res[0]:,.2f} ×{res[1]}  ({(res[0]-spot)/spot*100:+.2f}%)")
    if sup:
        lines.append(f"Sup: {sup[0]:,.2f} ×{sup[1]}  ({(sup[0]-spot)/spot*100:+.2f}%)")
    lines.append(f"CVD: {cvd['direction']}  (slope {cvd['slope_recent']:+.0f})")
    if cvd.get("divergence", "none") != "none":
        lines.append(f"      ⚠️ {cvd['divergence']} divergence")
    lines.append(f"Funding: {funding_apr:+.1f}% APR")
    lines.append("")
    lines.append(trig.format_trigger_line(verdict))
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        "_proxy read — aggr.trade is still the entry gate_",
        f"🕐 {ts}",
    ]
    send_text("\n".join(lines))

    # ── Phase-0 log ──
    _log({
        "ts": ts, "symbol": symbol, "spot": spot,
        "state": verdict["state"], "side": verdict.get("side"),
        "level": verdict.get("level"),
        "cvd_direction": cvd["direction"], "cvd_slope": cvd["slope_recent"],
        "divergence": cvd.get("divergence", "none"),
        "price_slope": cvd.get("price_slope", 0.0),
        "funding_apr": round(funding_apr, 2),
        "reason": verdict["reason"],
        "source": "gate",
    })
