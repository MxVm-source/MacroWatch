# bot/modules/tradewatch.py
"""
TradeWatch — enriched trade-plan alert.

PositionWatch (main.py) already fires a bare "Position Opened" alert off the
live Bitget account. TradeWatch enriches that single event with the full plan:
direction, entry, SL, the multi-TP ladder, R:R per TP, risk % of capital,
a liquidation sanity check, and the ratchet plan.

Design:
  - No separate account poller. main.py calls `on_position_opened(...)` from
    the existing PositionWatch open branch.
  - Because TP/SL bracket orders are often placed a beat AFTER the position
    opens, we re-poll the bracket once after TRADEWATCH_DELAY_S before posting.
  - Text-only (send_text). No image generation — memory-constrained host.

Risk model (matches the desk's guardrails):
  risk % of capital = (|entry - SL| / entry) * leverage * 100
  Flagged when it exceeds RISK_FLAG_PCT (~20–25%).
  SL must sit INSIDE the liquidation price; we check and flag if not.
"""

import os
import threading
import logging

from bot.utils import send_text
from bot.datafeed_bitget import (
    _fetch_current_futures_position,
    _fetch_pending_tp_sl_orders,
    _fetch_current_futures_position_elite,
    _fetch_pending_tp_sl_orders_elite,
    _position_is_open,
    _to_float,
    iso_utc_now,
    BITGET_API_KEY,
    ELITE_API_KEY,
    get_ticker,
)

# account label → (position_fetcher, order_fetcher)
_ACCOUNTS = {
    "main":  (_fetch_current_futures_position,       _fetch_pending_tp_sl_orders),
    "elite": (_fetch_current_futures_position_elite, _fetch_pending_tp_sl_orders_elite),
}

log = logging.getLogger("tradewatch")

ENABLED        = os.getenv("ENABLE_TRADEWATCH", "true").lower() in ("1", "true", "yes", "on")
DELAY_S        = float(os.getenv("TRADEWATCH_DELAY_S", "4"))   # wait for bracket orders to land
RISK_FLAG_PCT  = float(os.getenv("TRADEWATCH_RISK_FLAG_PCT", "22"))


# ─── Math ──────────────────────────────────────────────────────────────────

def _liq_price(pos: dict) -> float:
    return _to_float(pos.get("liquidationPrice") or pos.get("liqPx") or 0)


def compute_plan(side: str, entry: float, sl: float, tps: list,
                 lev: float, liq: float) -> dict:
    """Pure function — all the numbers, no I/O. side is LONG/SHORT."""
    is_long = side == "LONG"
    out = {
        "risk_pct": None, "risk_flag": False,
        "rr": [], "liq_ok": None, "liq_dist_pct": None,
        "sl_dist_pct": None,
    }

    if entry and sl:
        sl_dist = abs(entry - sl) / entry
        out["sl_dist_pct"] = sl_dist * 100
        if lev:
            risk = sl_dist * lev * 100
            out["risk_pct"]  = risk
            out["risk_flag"] = risk > RISK_FLAG_PCT

        risk_per_unit = abs(entry - sl)
        for tp in tps:
            if tp and risk_per_unit:
                reward = abs(tp - entry)
                out["rr"].append((tp, reward / risk_per_unit))

    # Liq check: for a LONG, SL must be ABOVE liq; for a SHORT, BELOW liq.
    if entry and sl and liq:
        out["liq_dist_pct"] = abs(entry - liq) / entry * 100
        out["liq_ok"] = (sl > liq) if is_long else (sl < liq)

    return out


# ─── Message ─────────────────────────────────────────────────────────────────

def build_plan_message(symbol: str, side: str, entry: float, size,
                       lev: float, sl: float, tps: list, liq: float,
                       account: str = "main") -> str:
    p = compute_plan(side, entry, sl, tps, lev, liq)
    side_emoji = "🟢" if side == "LONG" else "🔴"

    lines = [
        f"📋 *TradeWatch — Plan* ({account})",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Pair: {symbol}",
        f"Side: {side_emoji} {side}   Lev: {lev:g}x",
        f"Entry: {entry:,.2f}   Size: {size}",
    ]

    if sl:
        sl_line = f"SL: {sl:,.2f}"
        if p["sl_dist_pct"] is not None:
            sl_line += f"  ({p['sl_dist_pct']:.2f}% away)"
        lines.append(sl_line)
    else:
        lines.append("SL: ⚠️ none set")

    # TP ladder with R:R
    if p["rr"]:
        lines.append("")
        for i, (tp, rr) in enumerate(p["rr"], 1):
            lines.append(f"TP{i}: {tp:,.2f}   R:R {rr:.2f}")
    elif not tps:
        lines.append("TP: ⚠️ none set yet")

    # Risk %
    if p["risk_pct"] is not None:
        flag = "  🚩 OVER LIMIT" if p["risk_flag"] else ""
        lines += ["", f"Risk: {p['risk_pct']:.1f}% of capital{flag}"]

    # Liquidation check
    if liq:
        if p["liq_ok"] is True:
            liq_note = f"Liq: {liq:,.2f}  ✅ SL inside liq ({p['liq_dist_pct']:.1f}% away)"
        elif p["liq_ok"] is False:
            liq_note = f"Liq: {liq:,.2f}  ❌ SL BEYOND LIQ — fix sizing"
        else:
            liq_note = f"Liq: {liq:,.2f}"
        lines.append(liq_note)
    else:
        lines.append("Liq: ⚠️ unavailable")

    # Ratchet plan
    lines += [
        "",
        "_Ratchet:_ TP1→BE · TP2→TP1 · runner trails",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🕐 {iso_utc_now()}",
    ]
    return "\n".join(lines)


# ─── Entry points ──────────────────────────────────────────────────────────

def _post_for_symbol(symbol: str, account: str = "main"):
    """Re-poll the live position + bracket on the given account, then post the plan."""
    fetch_pos, fetch_orders = _ACCOUNTS.get(account, _ACCOUNTS["main"])

    pos = fetch_pos(symbol)
    if not _position_is_open(pos):
        log.info(f"TradeWatch: {symbol} ({account}) no longer open, skipping plan")
        return

    orders = fetch_orders(symbol) or {}
    tps = sorted(_to_float(x) for x in (orders.get("tp") or []))
    sls = sorted(_to_float(x) for x in (orders.get("sl") or []))

    side  = (pos.get("holdSide") or "").upper()
    entry = _to_float(pos.get("openPriceAvg") or pos.get("openPrice") or 0)
    size  = _to_float(pos.get("total") or pos.get("available") or 0)
    lev   = _to_float(pos.get("leverage") or 0)
    liq   = _liq_price(pos)

    # For a LONG the protective SL is the lowest stop; for a SHORT the highest.
    sl = (min(sls) if side == "LONG" else max(sls)) if sls else 0.0
    # Order TPs in the direction price travels.
    tps = tps if side == "LONG" else sorted(tps, reverse=True)

    send_text(build_plan_message(symbol, side, entry, size, lev, sl, tps, liq, account))


def on_position_opened(symbol: str, account: str = "main"):
    """
    Called by PositionWatch the moment it detects a new position.
    Schedules a delayed post so TP/SL bracket orders have time to land.
    """
    if not ENABLED:
        return
    threading.Timer(DELAY_S, lambda: _safe(_post_for_symbol, symbol, account)).start()


def show_plan(symbol: str = ""):
    """Manual /plan command - post plan for open position(s).

    If a symbol is given, looks for it on both accounts.
    If no symbol is given, scans ALL open positions across both accounts
    and posts a plan for each one found.
    """
    sym = (symbol or "").strip().upper()
    posted = False

    if sym:
        # Specific symbol: try on both accounts
        for acct in ("elite", "main"):
            if acct == "main"  and not BITGET_API_KEY:
                continue
            if acct == "elite" and not ELITE_API_KEY:
                continue
            try:
                fetch_pos, _ = _ACCOUNTS[acct]
                if _position_is_open(fetch_pos(sym)):
                    _safe(_post_for_symbol, sym, acct)
                    posted = True
            except Exception as e:
                log.warning(f"TradeWatch show_plan {acct}: {e}")
        if not posted:
            send_text(f"📋 [TradeWatch] No open {sym} position found on elite/main.")
        return

    # No symbol: scan every common pair across both accounts
    scan_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    for acct in ("elite", "main"):
        if acct == "main"  and not BITGET_API_KEY:
            continue
        if acct == "elite" and not ELITE_API_KEY:
            continue
        for s in scan_symbols:
            try:
                fetch_pos, _ = _ACCOUNTS[acct]
                if _position_is_open(fetch_pos(s)):
                    _safe(_post_for_symbol, s, acct)
                    posted = True
            except Exception as e:
                log.warning(f"TradeWatch show_plan scan {acct} {s}: {e}")

    if not posted:
        send_text("📋 [TradeWatch] No open positions found on elite/main.")


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        log.warning(f"TradeWatch error: {e}")
        try:
            send_text(f"📋 [TradeWatch] error: {str(e)[:160]}")
        except Exception:
            pass


# ─── Live state commands (/bot and /live) ────────────────────────────────────

def _format_position_card(symbol: str, pos: dict, orders: dict) -> str:
    """Build a compact live-state card for one open position."""
    side  = (pos.get("holdSide") or "").upper()
    entry = _to_float(pos.get("openPriceAvg") or pos.get("openPrice") or 0)
    size  = _to_float(pos.get("total") or pos.get("available") or 0)
    lev   = _to_float(pos.get("leverage") or 0)
    liq   = _liq_price(pos)

    tps = sorted(_to_float(x) for x in (orders.get("tp") or []))
    sls = sorted(_to_float(x) for x in (orders.get("sl") or []))
    sl  = (min(sls) if side == "LONG" else max(sls)) if sls else 0.0
    if side == "SHORT":
        tps = sorted(tps, reverse=True)

    # Live unrealized PnL
    last_px  = 0.0
    try:
        last_px = float(get_ticker(symbol) or 0)
    except Exception:
        pass

    pnl_line = ""
    if entry and last_px and lev:
        raw = (last_px - entry) / entry * 100
        if side == "SHORT":
            raw = -raw
        leveraged = raw * lev
        sign  = "🟢 +" if leveraged >= 0 else "🔴 "
        pnl_line = f"\nUnrealized: {sign}{leveraged:.2f}% (@ ${last_px:,.2f})"

    p = compute_plan(side, entry, sl, tps, lev, liq)
    side_emoji = "🟢" if side == "LONG" else "🔴"

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Pair: {symbol}",
        f"Side: {side_emoji} {side}   Lev: {lev:g}x",
        f"Entry: {entry:,.2f}   Size: {size}",
    ]

    if sl:
        sl_text = f"SL: {sl:,.2f}"
        if p["sl_dist_pct"] is not None:
            sl_text += f"  ({p['sl_dist_pct']:.2f}% away)"
        lines.append(sl_text)

    if p["rr"]:
        for i, (tp, rr) in enumerate(p["rr"], 1):
            lines.append(f"TP{i}: {tp:,.2f}   R:R {rr:.2f}")

    if p["risk_pct"] is not None:
        flag = "  🚩 OVER LIMIT" if p["risk_flag"] else ""
        lines.append(f"Risk: {p['risk_pct']:.1f}% of capital{flag}")

    if liq:
        if p["liq_ok"] is True:
            lines.append(f"Liq: {liq:,.2f}  ✅ ({p['liq_dist_pct']:.1f}% away)")
        elif p["liq_ok"] is False:
            lines.append(f"Liq: {liq:,.2f}  ❌ SL BEYOND LIQ")
        else:
            lines.append(f"Liq: {liq:,.2f}")

    if pnl_line:
        lines.append(pnl_line.lstrip("\n"))

    return "\n".join(lines)


def _show_account_state(account: str, label: str, scan_symbols: list[str]):
    """Render and send all open positions for one account."""
    if account == "main" and not BITGET_API_KEY:
        send_text(f"{label}: BITGET_API_KEY not set.")
        return
    if account == "elite" and not ELITE_API_KEY:
        send_text(f"{label}: ELITE_API_KEY not set.")
        return

    fetch_pos, fetch_orders = _ACCOUNTS[account]
    open_positions = []

    for sym in scan_symbols:
        try:
            pos = fetch_pos(sym)
            if _position_is_open(pos):
                orders = fetch_orders(sym) or {}
                open_positions.append((sym, pos, orders))
        except Exception as e:
            log.warning(f"{label} state scan {sym}: {e}")

    if not open_positions:
        send_text(f"*{label} — Live State*\n\n_No open positions. Flat._\n\n🕐 {iso_utc_now()}")
        return

    lines = [f"*{label} — Live State*", ""]
    for sym, pos, orders in open_positions:
        lines.append(_format_position_card(sym, pos, orders))
        lines.append("")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🕐 {iso_utc_now()}")
    send_text("\n".join(lines))


def show_bot_state():
    """/bot — current ATRb v2 systematic position(s) on main/sub-account."""
    _show_account_state("main", "🤖 ATRb v2", ["ETHUSDT"])


def show_live_state():
    """/live — current Maxime LIVE discretionary position(s) on Elite account."""
    _show_account_state("elite", "🎯 Maxime LIVE", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
