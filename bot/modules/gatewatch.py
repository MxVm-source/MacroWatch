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

# Same file market_structure._log_trigger_fire / /cvd_log already use.
CVD_LOG_PATH = os.getenv("CVD_LOG_PATH", "/var/data/cvd_trigger_log.jsonl")

# ── Auto-propose config (the scan that posts a stage card when aligned) ──────
GATE_SCAN_SYMBOLS = [s.strip().upper() for s in
                     os.getenv("GATE_SCAN_SYMBOLS", "BTCUSDT").split(",") if s.strip()]
SL_BUFFER      = float(os.getenv("GATE_SL_BUFFER", "0.005"))     # 0.5% beyond level
GATE_LEV       = float(os.getenv("GATE_LEV", "10"))
GATE_CAPITAL   = float(os.getenv("GATE_CAPITAL", "500"))         # capital-first base
GATE_SIZE_DEC  = int(os.getenv("BITGET_SIZE_DECIMALS", "4"))
GRADE_ROOM_R   = float(os.getenv("GATE_GRADE_ROOM_R", "2.0"))    # >=2R to TP1 = A-grade
GATE_AUTO_SCAN = os.getenv("GATE_AUTO_SCAN", "true").lower() in ("1", "true", "yes", "on")

# debounce: one auto-propose per (side, level) arrival, per symbol
_last_go: dict = {}


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


# ─── Auto-propose: scan → build plan → post a stage card ─────────────────────

def _build_auto_plan(symbol: str, verdict: dict, structure: dict):
    """
    Turn a LIVE/intrabar verdict into a full proposed plan from structure.
      entry = the level · SL = buffer beyond it · TPs = next levels in
      direction · size = capital-first (capital × lev / entry) · grade by room.
    Returns a plan dict, or None if there's no structural target to aim at.
    """
    side  = "SHORT" if verdict["side"] == "short" else "LONG"
    entry = float(verdict["level"])
    sl    = entry * (1 + SL_BUFFER) if side == "SHORT" else entry * (1 - SL_BUFFER)

    if side == "SHORT":
        targets = [float(p) for (p, _t) in structure.get("sup_levels", []) if p < entry][:3]
    else:
        targets = [float(p) for (p, _t) in structure.get("res_levels", []) if p > entry][:3]
    if not targets:
        return None   # no defined target in direction — don't propose a target-less trade

    r_sl  = abs(entry - sl)
    r_tp1 = abs(targets[0] - entry)
    grade = "A" if (r_sl and r_tp1 / r_sl >= GRADE_ROOM_R) else "B"
    size  = round(GATE_CAPITAL * GATE_LEV / entry, GATE_SIZE_DEC)

    return {
        "symbol":     symbol,
        "side":       side,
        "entry":      round(entry, 2),
        "sl":         round(sl, 2),
        "tps":        [round(x, 2) for x in targets],
        "total_size": size,
        "lev":        GATE_LEV,
        "grade":      grade,
    }


def _scan_one(symbol: str):
    structure = _get_structure(symbol)
    verdict   = trig.evaluate(structure, symbol=symbol)
    state     = verdict["state"]

    # Only a with-trend, intrabar GO auto-proposes. Counter-trend (await_4h),
    # NO_TAKE and MID_RANGE do not — they stay on the 4H path.
    if state != "LIVE" or verdict.get("entry_mode") != "intrabar":
        if state == "MID_RANGE":
            _last_go.pop(symbol, None)   # left the level → allow a fresh propose later
        return

    key = (verdict.get("side"), verdict.get("level"))
    if _last_go.get(symbol) == key:
        return   # already proposed this arrival

    plan = _build_auto_plan(symbol, verdict, structure)
    if not plan:
        return

    _last_go[symbol] = key
    _log({
        "ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
        "spot": verdict["spot"], "state": state, "side": verdict["side"],
        "level": verdict["level"], "cvd_direction": verdict["cvd"]["direction"],
        "divergence": verdict["cvd"].get("divergence", "none"),
        "reason": verdict["reason"], "source": "auto_propose",
    })

    try:
        from bot.modules import stagewatch
        stagewatch.stage_auto(plan, verdict=verdict)
    except Exception as e:
        log.warning(f"auto-propose stage failed for {symbol}: {e}")


def scan():
    """Scheduler entry — intrabar with-trend GO watch across GATE_SCAN_SYMBOLS."""
    if not GATE_AUTO_SCAN:
        return
    for sym in GATE_SCAN_SYMBOLS:
        try:
            _scan_one(sym)
        except Exception as e:
            log.warning(f"gate scan {sym}: {e}")


# ─── /scandiag : on-demand "what is the scan seeing right now" ────────────────

def scan_report(symbol: str = "BTCUSDT"):
    """
    Read-only. Mirrors _scan_one's decision path and reports exactly what the
    scanner sees this second and why it is / isn't auto-proposing. Places no
    orders, mutates no state.
    """
    symbol = (symbol or "BTCUSDT").upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    structure = _get_structure(symbol)
    verdict   = trig.evaluate(structure, symbol=symbol)
    state = verdict["state"]
    em    = verdict.get("entry_mode")
    side  = verdict.get("side")
    level = verdict.get("level")
    spot  = verdict["spot"]
    cvd   = verdict["cvd"]

    lines = [
        f"🔍 *SCANDIAG — {symbol}*",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"spot: {spot:,.2f}   regime: {verdict.get('regime', '?')}",
        f"CVD: {cvd['direction']} (slope {cvd['slope_recent']:+.0f})",
        f"state: {state}   side: {side or '—'}   mode: {em or '—'}",
        f"level: {level:,.0f}" if level else "level: —",
        f"scan: {'ON' if GATE_AUTO_SCAN else 'OFF (GATE_AUTO_SCAN=false)'}",
    ]

    # Mirror _scan_one exactly so the reported reason is the true one.
    if state != "LIVE" or em != "intrabar":
        if state == "MID_RANGE":
            why = "mid-range — no validated level in play"
        elif state == "NO_TAKE":
            why = f"at level but CVD failed the gate ({cvd['direction']})"
        elif em == "await_4h":
            why = "counter-trend / first-touch-after-flush — awaits 4H, no auto-open"
        else:
            why = state
        lines.append(f"→ NO propose: {why}")
        send_text("\n".join(lines))
        return

    plan = _build_auto_plan(symbol, verdict, structure)
    if not plan:
        lines.append("→ NO propose: no structural target in the trade direction")
        send_text("\n".join(lines))
        return

    if _last_go.get(symbol) == (side, level):
        lines.append(f"→ already proposed this arrival (debounced): {side} @ {level:,.0f}")
        send_text("\n".join(lines))
        return

    # the same active-plan guard stagewatch._post_stage applies
    blocked = None
    try:
        from bot.modules import stagewatch
        _pid, ap = stagewatch._active_plan_for(symbol)
        if ap:
            blocked = ap.get("state")
    except Exception:
        pass

    if blocked:
        lines.append(f"→ BLOCKED: an active {blocked} plan exists — run /flatten {symbol}")
    else:
        lines.append(f"→ WOULD propose NOW: {plan['side']} @ {plan['entry']:,.0f} · "
                     f"SL {plan['sl']:,.0f} · TPs {plan['tps']} · {plan['grade']}-grade")
    send_text("\n".join(lines))
