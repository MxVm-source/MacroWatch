# bot/modules/stagewatch.py
"""
stagewatch.py — the human-approved execution loop.

Flow:
  /stage ...            -> build a proposed plan, persist it, post a card with
                           [✅ Approve] [❌ Skip] buttons
  tap Approve           -> RE-CHECK guardrails live (price still at level, risk
                           ok, no correlated leg), then place the resting entry
                           on the ELITE account via bitget_exec
  entry fills           -> place laddered TPs; SL already preset on the entry
  TP fills (poll-diff)  -> ratchet the position SL (BE -> TP1), tighter-only
  position closes       -> mark CLOSED

Nothing here fires real orders unless bitget_exec.STAGE_LIVE is true. Until then
every Approve is a full dry-run: you see the exact loop, no money moves.

State persists to disk so a restart never loses an armed plan.
"""

import os
import json
import time
import uuid
import logging
from datetime import datetime, timezone, timedelta

from bot.utils import send_text, send_buttons, edit_message_text, answer_callback_query
from bot.datafeed_bitget import (
    get_ticker,
    _fetch_all_futures_positions_elite,
    _position_is_open,
    _to_float,
    iso_utc_now,
)
from bot.modules.tradewatch import compute_plan
from bot.modules import bitget_exec

try:
    from bot.modules.cvd import get_cvd
except Exception:  # cvd is optional for the card
    get_cvd = None

log = logging.getLogger("stagewatch")

STORE_PATH      = os.getenv("STAGE_STORE_PATH", "/tmp/macrowatch_stage.json")
EXPIRE_MIN      = int(os.getenv("STAGE_EXPIRE_MIN", "240"))   # plan TTL (4h default)
ENTRY_DRIFT_PCT = float(os.getenv("STAGE_ENTRY_DRIFT_PCT", "0.6"))  # price must stay this close
BE_BUFFER_FRAC  = float(os.getenv("STAGE_BE_BUFFER_FRAC", "0.0005"))  # fee buffer on BE
CORR_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
GRADE_SPLITS    = {"A": (0.30, 0.40, 0.30), "B": (0.50, 0.30, 0.20)}


# ─── Persistence ─────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(store: dict):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, STORE_PATH)   # atomic


def _active_plan_for(symbol: str):
    store = _load()
    for pid, p in store.items():
        if p["symbol"] == symbol and p["state"] in ("ARMED", "OPEN", "TP1", "TP2", "RUNNER"):
            return pid, p
    return None, None


# ─── /stage command ──────────────────────────────────────────────────────────

def _parse_stage(text_raw: str) -> dict:
    """
    /stage BTC short 67250 sl 67900 tps 65718,65081,64233 size 0.05 lev 10 grade A
    """
    t = text_raw.split()
    if len(t) < 4:
        raise ValueError("usage: /stage SYM long|short ENTRY sl SL tps T1,T2,T3 size QTY [lev N] [grade A|B]")

    sym  = t[1].upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    side = t[2].upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be long or short")
    entry = float(t[3])

    def _kw(key, default=None):
        if key in t:
            return t[t.index(key) + 1]
        return default

    sl    = float(_kw("sl"))
    tps   = [float(x) for x in (_kw("tps") or "").split(",") if x]
    size  = float(_kw("size"))
    lev   = float(_kw("lev", "10"))
    grade = (_kw("grade", "A") or "A").upper()
    if grade not in GRADE_SPLITS:
        grade = "A"
    return {"symbol": sym, "side": side, "entry": entry, "sl": sl,
            "tps": tps, "total_size": size, "lev": lev, "grade": grade}


def _approx_liq(side: str, entry: float, lev: float) -> float:
    if not lev:
        return 0.0
    return entry * (1 - 1 / lev) if side == "LONG" else entry * (1 + 1 / lev)


def _build_card(p: dict) -> str:
    liq = _approx_liq(p["side"], p["entry"], p["lev"])
    plan = compute_plan(p["side"], p["entry"], p["sl"], p["tps"], p["lev"], liq)
    emoji = "🟢" if p["side"] == "LONG" else "🔴"

    lines = [
        f"📋 *STAGE — awaiting approval* ({p['grade']}-grade)",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Pair: {p['symbol']}",
        f"Side: {emoji} {p['side']}   Lev: {p['lev']:g}x",
        f"Entry: {p['entry']:,.2f} (limit AT level)   Size: {p['total_size']}",
        f"SL: {p['sl']:,.2f}  ({plan['sl_dist_pct']:.2f}% away)" if plan["sl_dist_pct"] else "SL: —",
    ]
    splits = GRADE_SPLITS[p["grade"]]
    for i, (tp, rr) in enumerate(plan["rr"], 1):
        pct = int(splits[i - 1] * 100) if i <= len(splits) else 0
        lines.append(f"TP{i}: {tp:,.2f}   R:R {rr:.2f}   ({pct}%)")

    if plan["risk_pct"] is not None:
        flag = "  🚩 OVER LIMIT" if plan["risk_flag"] else ""
        lines.append(f"Risk: {plan['risk_pct']:.1f}% of capital{flag}")
    lines.append(f"Liq (approx): {liq:,.2f}")

    # CVD read on the card
    if get_cvd:
        try:
            c = get_cvd(p["symbol"])
            cvd_line = f"CVD: {c.direction}"
            if c.divergence != "none":
                cvd_line += f"  ⚠️ {c.divergence} divergence"
            lines.append(cvd_line)
        except Exception:
            pass

    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{'🔴 LIVE' if bitget_exec.is_live() else '🧪 DRY-RUN'} · expires in {EXPIRE_MIN}m",
        f"🕐 {iso_utc_now()}",
    ]
    return "\n".join(lines)


def stage(text_raw: str):
    """Handle the /stage command: build + persist + post the approval card."""
    p = _parse_stage(text_raw)

    # one active plan per symbol
    pid_existing, _ = _active_plan_for(p["symbol"])
    if pid_existing:
        send_text(f"⛔ A {p['symbol']} plan is already active. /flatten or let it close first.")
        return

    pid = uuid.uuid4().hex[:10]
    p.update({
        "plan_id":   pid,
        "account":   "elite",
        "state":     "STAGED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=EXPIRE_MIN)).isoformat(),
        "cur_sl":    p["sl"],
        "entry_order_id": None,
        "msg_id":    None,
    })

    card = _build_card(p)
    buttons = [[("✅ Approve", f"approve:{pid}"), ("❌ Skip", f"skip:{pid}")]]
    resp = send_buttons(card, buttons)
    try:
        p["msg_id"] = resp["result"]["message_id"]
    except Exception:
        pass

    store = _load()
    store[pid] = p
    _save(store)


# ─── Callback (button tap) ───────────────────────────────────────────────────

def handle_callback(cb: dict):
    """Routed from main.command_loop for any callback_query update."""
    cb_id = cb.get("id")
    data  = cb.get("data") or ""
    msg   = cb.get("message") or {}
    msg_id = msg.get("id") or msg.get("message_id")

    answer_callback_query(cb_id)   # ack fast so the button stops spinning

    if ":" not in data:
        return
    action, pid = data.split(":", 1)
    store = _load()
    p = store.get(pid)
    if not p:
        edit_message_text(msg_id, "⚠️ Plan not found / already handled.")
        return

    if action == "skip":
        p["state"] = "CANCELLED"
        _save(store)
        edit_message_text(msg_id, "❌ Skipped.")
        return

    if action != "approve":
        return
    if p["state"] != "STAGED":
        edit_message_text(msg_id, f"⚠️ Already {p['state'].lower()} — ignoring tap.")
        return

    # ── tap-time guardrail re-check ──────────────────────────────────────────
    block = _recheck(p)
    if block:
        edit_message_text(msg_id, f"⛔ Blocked at approval: {block}")
        return

    # ── arm: place the resting entry ─────────────────────────────────────────
    try:
        bitget_exec.place_entry(
            symbol=p["symbol"], side=p["side"], entry=p["entry"],
            size=p["total_size"], preset_sl=p["sl"], client_oid=p["plan_id"],
        )
        p["state"] = "ARMED"
        _save(store)
        tag = "🔴 LIVE" if bitget_exec.is_live() else "🧪 DRY-RUN"
        edit_message_text(msg_id,
            f"🟢 ARMED ({tag})\n{p['symbol']} {p['side']} entry {p['entry']:,.2f} resting · "
            f"SL {p['sl']:,.2f} preset · TPs stage on fill")
    except Exception as e:
        edit_message_text(msg_id, f"⛔ Arm failed: {str(e)[:160]}")


def _recheck(p: dict) -> str:
    """Return a block-reason string, or '' if all guardrails pass."""
    # expiry
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(p["expires_at"]):
            return "plan expired"
    except Exception:
        pass

    # price still at the level
    px = get_ticker(p["symbol"]) or 0.0
    if px:
        drift = abs(px - p["entry"]) / p["entry"] * 100
        if drift > ENTRY_DRIFT_PCT:
            return f"price moved {drift:.2f}% off the level ({px:,.2f}) — not at structure anymore"

    # risk cap
    liq = _approx_liq(p["side"], p["entry"], p["lev"])
    plan = compute_plan(p["side"], p["entry"], p["sl"], p["tps"], p["lev"], liq)
    if plan.get("risk_flag"):
        return f"risk {plan['risk_pct']:.1f}% over limit"

    # correlation: any other elite leg already open?
    try:
        for pos in _fetch_all_futures_positions_elite():
            sym = (pos.get("symbol") or "").upper()
            if sym in CORR_SYMBOLS and sym != p["symbol"] and _position_is_open(pos):
                return f"correlated leg already open ({sym}) — one book in a bear, not diversifying"
    except Exception:
        pass

    return ""


# ─── Ratchet state machine (driven by PositionWatch 10s poll-diff) ───────────

def on_position_change(symbol: str, prev: dict, cur: dict):
    """
    Called from main._poll_positions for the ELITE account on every diff.
    prev/cur are the snapshot dicts main already builds:
      {has_position, side, size, entry, lev, tp:[...], sl:[...]}
    """
    pid, p = _active_plan_for(symbol)
    if not p:
        return
    store = _load()

    # entry filled: ARMED -> OPEN, lay the TP ladder
    if p["state"] == "ARMED" and not prev.get("has_position") and cur.get("has_position"):
        splits = GRADE_SPLITS[p["grade"]]
        sizes = [round(p["total_size"] * s, 6) for s in splits[:len(p["tps"])]]
        try:
            bitget_exec.place_ladder_tps(symbol, p["side"], p["tps"], sizes, p["plan_id"])
        except Exception as e:
            send_text(f"⚠️ [Stage] {symbol} TP ladder failed: {str(e)[:120]}")
        p["state"] = "OPEN"
        store[pid] = p; _save(store)
        send_text(f"🟢 [Stage] {symbol} filled @ {cur.get('entry'):,.2f} — ladder live, SL preset.")
        return

    # closed
    if prev.get("has_position") and not cur.get("has_position"):
        p["state"] = "CLOSED"
        store[pid] = p; _save(store)
        return

    # TP hit -> ratchet (a TP price disappeared from the bracket)
    if cur.get("has_position") and prev.get("has_position"):
        prev_tps = set(prev.get("tp") or [])
        cur_tps  = set(cur.get("tp") or [])
        hits = prev_tps - cur_tps
        if not hits:
            return
        for _ in hits:
            _advance_ratchet(symbol, p)
        store[pid] = p; _save(store)


def _advance_ratchet(symbol: str, p: dict):
    side = p["side"]
    if p["state"] == "OPEN":          # TP1 hit -> SL to break-even
        be = p["entry"] * (1 - BE_BUFFER_FRAC) if side == "LONG" else p["entry"] * (1 + BE_BUFFER_FRAC)
        if _move_sl(symbol, p, be):
            p["state"] = "TP1"
            send_text(f"🔧 [Stage] {symbol} TP1 hit → SL to break-even {be:,.2f}")
    elif p["state"] == "TP1":         # TP2 hit -> SL to TP1
        tp1 = p["tps"][0]
        if _move_sl(symbol, p, tp1):
            p["state"] = "TP2"
            send_text(f"🔧 [Stage] {symbol} TP2 hit → SL to TP1 {tp1:,.2f}")
    elif p["state"] == "TP2":         # TP3 hit -> runner done / closed by TP
        p["state"] = "RUNNER"


def _move_sl(symbol: str, p: dict, new_sl: float) -> bool:
    """Tighter-only guard, then push the new whole-position SL."""
    cur = p.get("cur_sl")
    if cur is not None:
        if p["side"] == "LONG" and new_sl < cur:
            log.warning(f"{symbol} reject loosening SL {cur}->{new_sl}")
            return False
        if p["side"] == "SHORT" and new_sl > cur:
            log.warning(f"{symbol} reject loosening SL {cur}->{new_sl}")
            return False
    try:
        bitget_exec.set_position_sl(symbol, p["side"], round(new_sl, 2))
        p["cur_sl"] = round(new_sl, 2)
        return True
    except Exception as e:
        send_text(f"⚠️ [Stage] {symbol} SL ratchet failed: {str(e)[:120]}")
        return False


# ─── Kill switch ─────────────────────────────────────────────────────────────

def flatten_cmd(symbol: str = ""):
    sym = (symbol or "").strip().upper()
    if sym and not sym.endswith("USDT"):
        sym += "USDT"
    targets = [sym] if sym else CORR_SYMBOLS
    store = _load()
    for s in targets:
        try:
            bitget_exec.cancel_plan_orders(s)
            bitget_exec.flatten(s)
            pid, p = _active_plan_for(s)
            if p:
                p["state"] = "CLOSED"; store[pid] = p
            send_text(f"🛑 [Stage] flattened {s}")
        except Exception as e:
            send_text(f"⚠️ [Stage] flatten {s} failed: {str(e)[:120]}")
    _save(store)
