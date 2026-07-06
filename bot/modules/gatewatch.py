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
from bot.modules.cvd import get_cvd
from bot.modules.structural_stop import (
    structural_stop, honest_rr, grade_tps, MAX_RISK_PCT,
)
from bot.datafeed_bitget import get_elite_usdt_balance

log = logging.getLogger("gatewatch")

# Same file market_structure._log_trigger_fire / /cvd_log already use.
CVD_LOG_PATH = os.getenv("CVD_LOG_PATH", "/var/data/cvd_trigger_log.jsonl")

# ── Auto-propose config (the scan that posts a stage card when aligned) ──────
GATE_SCAN_SYMBOLS = [s.strip().upper() for s in
                     os.getenv("GATE_SCAN_SYMBOLS", "BTCUSDT").split(",") if s.strip()]
SL_BUFFER      = float(os.getenv("GATE_SL_BUFFER", "0.005"))     # 0.5% beyond level
GATE_LEV       = float(os.getenv("GATE_LEV", "10"))
GATE_RISK_PCT_A = float(os.getenv("GATE_RISK_PCT_A", "25"))      # % of Elite equity risked, A-grade
GATE_RISK_PCT_B = float(os.getenv("GATE_RISK_PCT_B", "15"))      # % of Elite equity risked, B-grade
GATE_EQUITY_FALLBACK = float(os.getenv("GATE_EQUITY_FALLBACK", "1000"))  # used if balance fetch fails
GATE_SIZE_DEC  = int(os.getenv("BITGET_SIZE_DECIMALS", "4"))
GRADE_ROOM_R   = float(os.getenv("GATE_GRADE_ROOM_R", "2.0"))    # best target >=2R = A-grade
MIN_TRADE_RR   = float(os.getenv("GATE_MIN_TRADE_RR", "1.0"))    # best target must clear this to propose
SOFT_RR_PREF   = float(os.getenv("GATE_SOFT_RR_PREF", "1.5"))    # warn (not block) below your real floor
WALL_TOUCHES   = int(os.getenv("GATE_WALL_TOUCHES", "3"))        # first target this strong = into-a-wall = B
BAND_PCT       = float(os.getenv("GATE_BAND_PCT", "0.012"))      # strong level within this above entry = sandwiched
GATE_AUTO_SCAN = os.getenv("GATE_AUTO_SCAN", "true").lower() in ("1", "true", "yes", "on")

# debounce: one auto-propose per (side, level) arrival, per symbol
_last_go: dict = {}

# why the last _build_auto_plan returned None, per symbol (for /scandiag honesty)
_block_reason: dict = {}

# ── Row-2 flush guard + Row-3 break-confirmation config ─────────────────────
FLUSH_PCT   = float(os.getenv("GATE_FLUSH_PCT", "1.5"))   # recent % move into a level = knife
BREAK_BUFFER = float(os.getenv("GATE_BREAK_BUFFER", "0.001"))  # 0.1% past the level
_break_state: dict = {}   # symbol -> {"ceiling": x, "floor": y}, refreshed each 4H close


def _flush_flag(cvd) -> bool:
    """
    First-touch-after-flush proxy: a large, fast recent price move (into the level).
    Reuses the CVD result's recent price move — no extra fetch. Conservative: over-
    flagging just means more WAIT-4H, which is the correct bias for knife-catches.
    """
    try:
        if cvd.last_price and cvd.price_slope:
            move_pct = abs(cvd.price_slope) / cvd.last_price * 100
            return move_pct >= FLUSH_PCT
    except Exception:
        pass
    return False


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
    side_l = "short" if side == "SHORT" else "long"

    # Full S/R set with touch counts — the structural stop needs the real walls.
    levels = [(float(p), int(t)) for (p, t) in
              (structure.get("res_levels", []) + structure.get("sup_levels", []))]
    entry_touches = next((t for (p, t) in levels if abs(p - entry) < 0.01), None)

    # SL beyond the next >=3-touch wall (skips thin levels that just get wicked),
    # NOT a fixed % that lands in the chop and inflates R:R.
    try:
        sr       = structural_stop(entry, side_l, levels, GATE_LEV)
        sl       = sr.sl
        risk_pct = sr.risk_pct
        warnings = list(sr.warnings)
    except Exception as e:
        sl       = entry * (1 + SL_BUFFER) if side == "SHORT" else entry * (1 - SL_BUFFER)
        risk_pct = SL_BUFFER * GATE_LEV
        warnings = [f"structural stop unavailable ({e}); fell back to {SL_BUFFER:.1%} buffer"]

    if side == "SHORT":
        tgt = [(float(p), int(t)) for (p, t) in structure.get("sup_levels", []) if p < entry][:3]
    else:
        tgt = [(float(p), int(t)) for (p, t) in structure.get("res_levels", []) if p > entry][:3]
    if not tgt:
        _block_reason[symbol] = "no S/R target in the trade direction"
        return None   # no defined target in direction — don't propose a target-less trade
    targets     = [p for p, _ in tgt]
    tp1_touches = tgt[0][1]

    # Honest R:R measured against the REAL stop, not a tight % one.
    rr = honest_rr(entry, sl, targets, side_l)
    warnings += grade_tps(rr)

    best_rr = max(rr) if rr else 0.0
    # TP1 is your close scalp leg — SUPPOSED to be sub-1R. Gate on whether a DEEPER
    # target clears your real reward floor, not on TP1. grade_tps still warns on card.
    if best_rr < MIN_TRADE_RR:
        _block_reason[symbol] = (f"best target only {best_rr:.2f}R vs structural stop {sl:,.0f} "
                                 f"(< {MIN_TRADE_RR}R floor) — no real reward")
        log.info(f"auto-plan blocked {symbol}: best {best_rr:.2f}R < {MIN_TRADE_RR}R floor")
        return None
    if risk_pct > MAX_RISK_PCT:
        _block_reason[symbol] = f"risk {risk_pct:.0%} > {MAX_RISK_PCT:.0%} guardrail (SL {sl:,.0f})"
        log.info(f"auto-plan blocked {symbol}: risk {risk_pct:.1%} > {MAX_RISK_PCT:.0%} guardrail")
        return None

    _block_reason.pop(symbol, None)   # passed all gates
    if best_rr < SOFT_RR_PREF:
        warnings.append(f"best target {best_rr:.2f}R < your {SOFT_RR_PREF}R floor — marginal, judge it")

    # GRADE = how to scale, and it must see WHAT'S IN THE WAY, not just the deep R:R.
    # A-grade = trend-continuation into OPEN AIR (runner has room) → 30/40/30.
    # B-grade = first target is a real wall, or entry is sandwiched in a band, or low
    #           reward → front-load 50/30/20 (bank into the first obstacle).
    # Fixes the misgrade: a short whose TP1 sits on a high-touch support (a magnet that
    # fights the move) is NOT open air — it's B, no matter how far the deep target is.
    tp1_is_wall = tp1_touches >= WALL_TOUCHES
    if side == "SHORT":
        band = [t for (p, t) in structure.get("res_levels", [])
                if entry < p <= entry * (1 + BAND_PCT) and t >= WALL_TOUCHES]
    else:
        band = [t for (p, t) in structure.get("sup_levels", [])
                if entry * (1 - BAND_PCT) <= p < entry and t >= WALL_TOUCHES]
    sandwiched = len(band) > 0

    entry_thin = entry_touches is not None and entry_touches < WALL_TOUCHES

    if best_rr >= GRADE_ROOM_R and not tp1_is_wall and not sandwiched and not entry_thin:
        grade = "A"
    else:
        grade = "B"
        if entry_thin:
            warnings.append(f"entry level is only ×{entry_touches} touches (< {WALL_TOUCHES}) — "
                            f"capped at B regardless of room, per your entry-quality rule")
        if tp1_is_wall:
            warnings.append(f"TP1 sits on a ×{tp1_touches} wall — front-loaded 50/30/20, "
                            f"bank biggest into the first obstacle")
        elif sandwiched:
            warnings.append("entry sandwiched in a resistance band — front-loaded 50/30/20")
    target_pct = GATE_RISK_PCT_A if grade == "A" else GATE_RISK_PCT_B
    try:
        equity = get_elite_usdt_balance() or GATE_EQUITY_FALLBACK
    except Exception as e:
        equity = GATE_EQUITY_FALLBACK
        warnings.append(f"equity fetch failed ({e}); used ${GATE_EQUITY_FALLBACK:.0f} fallback")
    target_risk_dollar = equity * target_pct / 100
    # risk_pct here is a fraction (sl_dist_pct * leverage) from structural_stop —
    # it's the fraction of ALLOCATED CAPITAL at risk, not of equity. Solve backwards
    # for the capital that puts exactly target_risk_dollar of equity on the line.
    capital = (target_risk_dollar / risk_pct) if risk_pct > 0 else 0.0
    size    = round(capital * GATE_LEV / entry, GATE_SIZE_DEC)
    warnings.append(f"sized at {target_pct:.2f}% of ${equity:,.0f} equity "
                    f"(${target_risk_dollar:,.0f} risk) — {grade}-grade")

    return {
        "symbol":     symbol,
        "side":       side,
        "entry":      round(entry, 2),
        "sl":         round(sl, 2),
        "tps":        [round(x, 2) for x in targets],
        "rr":         rr,
        "risk_pct":   round(risk_pct, 4),
        "warnings":   warnings,
        "total_size": size,
        "lev":        GATE_LEV,
        "grade":      grade,
    }


def _scan_one(symbol: str):
    structure = _get_structure(symbol)
    cvd       = get_cvd(symbol)                      # fetch once, reuse for flush + gate
    flush     = _flush_flag(cvd)
    verdict   = trig.evaluate(structure, symbol=symbol, cvd=cvd, fresh_flush=flush)
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
        try:
            send_text(f"⚠️ [Stage] auto-propose for {symbol} failed to persist: {str(e)[:140]}")
        except Exception:
            pass


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
    cvd_r     = get_cvd(symbol)
    flush     = _flush_flag(cvd_r)
    verdict   = trig.evaluate(structure, symbol=symbol, cvd=cvd_r, fresh_flush=flush)
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
        f"state: {state.replace('_',' ')}   side: {side or '—'}   mode: {(em or '—').replace('_',' ')}",
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
        lines.append(f"→ NO propose: {_block_reason.get(symbol, 'no structural target in the trade direction')}")
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


# ─── Row 3: break confirmation on the 4H close (close_break wired) ───────────

def check_break(symbol: str = "BTCUSDT"):
    """
    Scheduler entry — run once per 4H close. Compares the close (spot just after
    the candle closes) against the PREVIOUS 4H's nearest range edges; a close
    beyond them = a confirmed break → 🚀 BREAKOUT / 🔻 BREAKDOWN alert.

    State is per-symbol and refreshed each call, so the same break never
    re-fires. In-memory: a restart re-seeds (one missed close at worst).
    """
    structure = _get_structure(symbol)
    spot = structure["spot"]

    cur_ceiling = _nearest(structure.get("res_levels", []), True,  spot)
    cur_floor   = _nearest(structure.get("sup_levels", []), False, spot)
    cur_ceiling = cur_ceiling[0] if cur_ceiling else None
    cur_floor   = cur_floor[0]   if cur_floor   else None

    prev = _break_state.get(symbol)
    _break_state[symbol] = {"ceiling": cur_ceiling, "floor": cur_floor}
    if not prev:
        return   # first run — seed state, nothing to compare against yet

    ceiling = prev.get("ceiling")
    floor   = prev.get("floor")
    # buffer so a hair-cross doesn't count as a break
    ceil_b  = ceiling * (1 + BREAK_BUFFER) if ceiling else None
    floor_b = floor   * (1 - BREAK_BUFFER) if floor   else None

    verdict = trig.close_break(spot, ceiling=ceil_b, floor=floor_b, symbol=symbol)
    if not verdict:
        return

    e = "🚀" if verdict["state"] == "BREAKOUT" else "🔻"
    send_text(
        f"{e} *{verdict['state']} — {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"4H close {spot:,.0f} {'>' if verdict['state']=='BREAKOUT' else '<'} "
        f"{verdict['level']:,.0f}\n"
        f"{verdict['reason']}\n"
        f"→ continuation {verdict['side']} — wait for retest, confirm on aggr\n"
        f"🕐 {datetime.now(timezone.utc).isoformat()}"
    )
    _log({
        "ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
        "spot": spot, "state": verdict["state"], "side": verdict["side"],
        "level": verdict["level"], "reason": verdict["reason"], "source": "break",
    })
