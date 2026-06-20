# bot/main.py  — build 2026-06-17-v5
"""
MacroWatch — Entry point

Polling architecture (all via APScheduler, no raw threads for polling):
  TrumpWatch   → every 60s
  FedWatch     → every 5min
  CryptoWatch  → weekly cron (Sunday 18:00)
  CryptoDaily  → daily cron  (15:28)
  PositionWatch → every 10s (open/close/TP/SL detection)

Command loop runs in a single daemon thread.
All poll functions are wrapped so one crash never kills the scheduler.
"""

import os
import sys
import json
import logging
import platform
import threading
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

# ─── Silence APScheduler's per-job INFO spam ─────────────────────────────────
# Only show WARNING and above — errors will still surface.
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.jobstores.default").setLevel(logging.WARNING)

from bot.utils import send_text, get_updates


import bot.modules.fedwatch        as fedwatch
import bot.modules.market_structure_module as market_structure
import bot.modules.stagewatch              as stagewatch
import bot.modules.gatewatch               as gatewatch
import bot.modules.trumpwatch_live as trumpwatch_live
import bot.modules.correlwatch     as correlwatch
import bot.modules.whalewatch      as whalewatch
import bot.modules.stratwatch      as stratwatch
import bot.modules.challengewatch  as challengewatch
import bot.modules.fundingwatch    as fundingwatch
import bot.modules.oiwatch         as oiwatch
import bot.modules.optionswatch    as optionswatch
import bot.modules.vixwatch        as vixwatch
import bot.modules.intelwatch      as intelwatch
import bot.modules.reportwatch     as reportwatch
import bot.modules.tradewatch      as tradewatch

log = logging.getLogger("main")

STARTED_AT_UTC = datetime.now(timezone.utc)

# ─── Public channel config ───────────────────────────────────────────────────
# PUBLIC_CHAT_ID used by challengewatch._send_to_both (ATRb v2 bot challenge)
# and the weekly brief / strategy recap / weekly intel deep dive — all of
# which read os.getenv("PUBLIC_CHAT_ID") directly in their own modules.
# main.py itself no longer sends to public directly.
PUBLIC_CHAT_ID = os.getenv("PUBLIC_CHAT_ID", "")

# ─── PositionWatch state ─────────────────────────────────────────────────────
# Tracks last known snapshot per symbol so we can detect changes.
# Initialised as None — first poll just seeds the baseline, no alerts.
from bot.datafeed_bitget import (
    _fetch_current_futures_position,
    _fetch_pending_tp_sl_orders,
    _position_is_open,
    _to_float,
    iso_utc_now,
    BITGET_SYMBOLS,
)

_POS_SNAPSHOT: dict = {}   # { "BTCUSDT": { has_position, side, size, entry, tp, sl }, ... }
_POS_INITIALISED = False

# ─── Trade streak tracker ─────────────────────────────────────────────────────
_STREAK: dict = {
    "count":     0,      # positive = win streak, negative = loss streak
    "last_side": None,   # "win" or "loss"
}

def _update_streak(is_win: bool) -> str:
    """Update streak and return a formatted streak line for the alert."""
    if is_win:
        _STREAK["count"] = max(_STREAK["count"], 0) + 1
        _STREAK["last_side"] = "win"
    else:
        _STREAK["count"] = min(_STREAK["count"], 0) - 1
        _STREAK["last_side"] = "loss"

    count = abs(_STREAK["count"])
    if _STREAK["last_side"] == "win":
        if count >= 5:
            return f"🔥 {count} win streak"
        elif count >= 2:
            return f"✅ {count} wins in a row"
        else:
            return ""   # single win — no streak line
    else:
        if count >= 3:
            return f"⚠️ {count} losses in a row"
        else:
            return ""

# ─── Scheduler (module-level so commands can inspect jobs) ───────────────────

SCHED = BackgroundScheduler(timezone=os.getenv("TIMEZONE", "Europe/Brussels"))


# ─── Safe job wrappers ───────────────────────────────────────────────────────
# Each wrapper catches its own errors so one broken module never kills others.

def _job_trumpwatch():
    try:
        trumpwatch_live.poll_once()
    except Exception as e:
        _err("TrumpWatch", e)

def _job_fedwatch():
    try:
        fedwatch.poll_once()
    except Exception as e:
        _err("FedWatch", e)

def _job_weekly_brief():
    try:
        from bot.modules.weeklybrief import send_weekly_brief
        send_weekly_brief(_get_modules())
    except Exception as e:
        _err("WeeklyBrief", e)


def _job_strategy_recap():
    try:
        from bot.modules.strategyrecap import send_strategy_recap
        send_strategy_recap()
    except Exception as e:
        _err("StrategyRecap", e)


def _job_weekly_intel():
    try:
        intelwatch.send_weekly_intel(_get_modules())
    except Exception as e:
        _err("WeeklyIntel", e)

def _err(module: str, exc: Exception):
    msg = f"⚠️ [{module}] Job error: {str(exc)[:200]}"
    print(msg, flush=True)
    try:
        send_text(msg)
    except Exception:
        pass


# ─── Scheduler setup ─────────────────────────────────────────────────────────

def _job_positionwatch():
    try:
        _poll_positions()
    except Exception as e:
        _err("PositionWatch", e)



def _poll_positions():
    """
    Dual-account PositionWatch.

    Main / sub-account (BITGET_API_KEY) — ATRb v2 systematic bot:
      Lightweight alerts only — Position Opened + Position Closed.
      No TradeWatch plan card (bot has its own internal risk discipline).
      No per-TP / per-SL alerts.

    Elite account (ELITE_API_KEY) — TraderWatch discretionary:
      Full alerts — Position Opened, Position Closed, TP Hit, SL Hit.
      TradeWatch enriched plan card (R:R, risk %, liq check, ratchet)
      fires automatically 4 seconds after position opens.

    Both feed to private group only. Snapshot keyed by (account, symbol)
    so the two books never collide.
    """
    global _POS_INITIALISED, _POS_SNAPSHOT

    from bot.datafeed_bitget import (
        _fetch_current_futures_position,
        _fetch_pending_tp_sl_orders,
        _fetch_current_futures_position_elite,
        _fetch_pending_tp_sl_orders_elite,
        BITGET_API_KEY,
        ELITE_API_KEY,
    )

    accounts = []
    if BITGET_API_KEY:
        accounts.append({
            "name":         "main",
            "label":        "🤖 ATRb v2",
            "fetch_pos":    _fetch_current_futures_position,
            "fetch_orders": _fetch_pending_tp_sl_orders,
            "symbols":      BITGET_SYMBOLS or ["ETHUSDT"],
            "rich":         False,  # lightweight alerts only
        })
    if ELITE_API_KEY:
        accounts.append({
            "name":         "elite",
            "label":        "🎯 TraderWatch",
            "fetch_pos":    _fetch_current_futures_position_elite,
            "fetch_orders": _fetch_pending_tp_sl_orders_elite,
            "symbols":      ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "rich":         True,   # TradeWatch plan + TP/SL alerts
        })

    for acct in accounts:
        for sym in acct["symbols"]:
            sym = sym.strip().upper()
            try:
                pos    = acct["fetch_pos"](sym)
                orders = acct["fetch_orders"](sym) or {}
                tps    = sorted([_to_float(x) for x in (orders.get("tp") or [])])
                sls    = sorted([_to_float(x) for x in (orders.get("sl") or [])])
                is_open = _position_is_open(pos)

                cur = {
                    "has_position": is_open,
                    "side":  (pos.get("holdSide") or "").upper() if pos else "",
                    "size":  _to_float(pos.get("total") or pos.get("available") or 0) if pos else 0.0,
                    "entry": _to_float(pos.get("openPriceAvg") or pos.get("openPrice") or 0) if pos else 0.0,
                    "lev":   pos.get("leverage", "?") if pos else "?",
                    "tp":    tps,
                    "sl":    sls,
                }

                key  = (acct["name"], sym)
                prev = _POS_SNAPSHOT.get(key)

                # First pass — seed baseline silently
                if not _POS_INITIALISED or prev is None:
                    _POS_SNAPSHOT[key] = cur
                    continue

                side_emoji = "🟢" if cur["side"] == "LONG" else "🔴"
                label      = acct["label"]
                rich       = acct["rich"]

                # ── Position opened ──────────────────────────────────────
                if not prev["has_position"] and cur["has_position"]:
                    cur["opened_at"] = datetime.now(timezone.utc)
                    send_text(
                        f"*{label} — Position Opened*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Pair: {sym}\n"
                        f"Side: {side_emoji} {cur['side']}\n"
                        f"Entry: {cur['entry']:.2f}\n"
                        f"Size: {cur['size']}\n"
                        f"Leverage: {cur['lev']}x\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🕐 {iso_utc_now()}"
                    )
                    if rich:
                        # Elite only — enriched TradeWatch plan card
                        tradewatch.on_position_opened(sym, account=acct["name"])

                # ── Position closed ──────────────────────────────────────
                elif prev["has_position"] and not cur["has_position"]:
                    prev_side  = prev.get("side") or "?"
                    prev_emoji = "🟢" if prev_side == "LONG" else "🔴"
                    entry      = prev.get("entry", 0.0)

                    # Fetch last price for PnL estimate
                    try:
                        from bot.datafeed_bitget import get_ticker
                        last_px = get_ticker(sym) or 0.0
                    except Exception:
                        last_px = 0.0

                    pnl_pct_line = ""
                    leveraged    = 0.0
                    if entry and last_px:
                        raw = (last_px - entry) / entry * 100
                        if prev_side == "SHORT":
                            raw = -raw
                        leveraged = raw * _to_float(prev.get("lev") or 1)
                        sign      = "🟢 +" if leveraged >= 0 else "🔴 "
                        pnl_pct_line = f"\nEst. PnL: {sign}{leveraged:.1f}% (@ {last_px:.2f})"

                    duration_line = ""
                    opened_at = prev.get("opened_at")
                    if opened_at:
                        delta = datetime.now(timezone.utc) - opened_at
                        h, rem = divmod(int(delta.total_seconds()), 3600)
                        m = rem // 60
                        duration_line = f"\nHeld: {h}h {m:02d}m"

                    streak_line = ""
                    if rich and entry and last_px:
                        s = _update_streak(leveraged >= 0)
                        if s:
                            streak_line = f"\n{s}"

                    send_text(
                        f"*{label} — Position Closed*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Pair: {sym}\n"
                        f"Side: {prev_emoji} {prev_side}"
                        f"{pnl_pct_line}"
                        f"{duration_line}"
                        f"{streak_line}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🕐 {iso_utc_now()}"
                    )

                # ── TP / SL hit detection (Elite only) ───────────────────
                elif rich and cur["has_position"] and prev["has_position"]:
                    prev_tps = prev.get("tp") or []
                    cur_tps  = cur.get("tp") or []
                    prev_sls = prev.get("sl") or []
                    cur_sls  = cur.get("sl") or []

                    # TP hit — a TP price disappeared
                    for tp in prev_tps:
                        if tp not in cur_tps:
                            send_text(
                                f"✅ *{label} — TP Hit*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Pair: {sym}\n"
                                f"Side: {side_emoji} {cur['side']}\n"
                                f"TP: {tp}\n"
                                f"🕐 {iso_utc_now()}"
                            )

                    # SL hit — a SL price disappeared and position still open
                    for sl in prev_sls:
                        if sl not in cur_sls:
                            send_text(
                                f"❌ *{label} — SL Moved/Hit*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Pair: {sym}\n"
                                f"Side: {side_emoji} {cur['side']}\n"
                                f"SL: {sl}\n"
                                f"🕐 {iso_utc_now()}"
                            )

                # Preserve opened_at across snapshots
                if cur["has_position"] and prev and prev.get("opened_at"):
                    cur["opened_at"] = prev["opened_at"]

                # Stage loop: drive the auto-ratchet off the elite diff
                if acct["name"] == "elite" and prev is not None:
                    try:
                        stagewatch.on_position_change(sym, prev, cur)
                    except Exception as e:
                        log.warning(f"stagewatch {sym}: {e}")

                _POS_SNAPSHOT[key] = cur

            except Exception as e:
                log.warning(f"PositionWatch {acct['name']} {sym}: {e}")

    # Mark initialised after first full pass
    if not _POS_INITIALISED:
        _POS_INITIALISED = True
        print("📘 PositionWatch baseline set ✅", flush=True)


# ─── Challenge updates ───────────────────────────────────────────────────────

def _job_challenge_update():
    """Tuesday 09:15 — fire both challenges to their respective channels."""
    # Bot challenge -> public + private
    try:
        challengewatch.show_bot_challenge()
    except Exception as e:
        _err("BotChallengeUpdate", e)

    # Live (TraderWatch discretionary) challenge -> private only
    try:
        challengewatch.show_live_challenge()
    except Exception as e:
        _err("LiveChallengeUpdate", e)


def _job_weekly_perf():
    try:
        _send_weekly_perf()
    except Exception as e:
        _err("WeeklyPerf", e)


def _send_weekly_perf():
    """
    Monday 09:00 — Weekly performance recap.
    Pulls closed trades from Bitget for the past 7 days and summarises results.
    Falls back to ETH price change if no API credentials.
    """
    from bot.datafeed_bitget import (
        _signed_request, _to_float,
        BITGET_PRODUCT_TYPE, BITGET_API_KEY
    )

    sym = os.getenv("INFINEX_SYMBOL", "ETHUSDT")
    now = datetime.now(timezone.utc)
    week_start_dt = now - timedelta(days=7)
    week_start_str = week_start_dt.strftime("%b %d")
    week_end_str   = (now - timedelta(days=1)).strftime("%b %d")

    # ── ETH 7D price context ─────────────────────────────────────────────────
    eth_line = ""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params={"symbol": sym, "granularity": "4H", "limit": "42",
                    "productType": BITGET_PRODUCT_TYPE},
            timeout=8,
        )
        data   = (r.json().get("data") or []) if r.ok else []
        closes = [float(row[4]) for row in data if isinstance(row, (list,tuple)) and len(row) >= 5]
        if closes:
            chg = (closes[-1] - closes[0]) / closes[0] * 100
            e   = "📈" if chg >= 0 else "📉"
            s   = "+" if chg >= 0 else ""
            eth_line = f"ETH/USDT: {e} {s}{chg:.1f}% this week\n"
    except Exception:
        pass

    # ── Closed trades (authenticated) ────────────────────────────────────────
    trades_section = ""
    if BITGET_API_KEY:
        try:
            start_ms = int(week_start_dt.timestamp() * 1000)
            end_ms   = int(now.timestamp() * 1000)

            res = _signed_request(
                "GET",
                "/api/v2/mix/order/history",
                params={
                    "symbol":      sym,
                    "productType": BITGET_PRODUCT_TYPE,
                    "startTime":   str(start_ms),
                    "endTime":     str(end_ms),
                    "limit":       "100",
                }
            )
            orders = ((res.get("data") or {}).get("orderList") or [])

            # Only filled closing orders with a realised PnL
            closed = []
            for o in orders:
                state     = (o.get("state") or "").lower()
                trade_side = (o.get("tradeSide") or o.get("side") or "").lower()
                pnl_raw   = o.get("pnl") or o.get("realizedPL") or o.get("profit") or ""
                if state != "filled":
                    continue
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue
                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue

                # Trade date
                try:
                    ctime = int(o.get("cTime") or o.get("uTime") or 0)
                    dt    = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc).strftime("%b %d")
                except Exception:
                    dt = "—"

                side = (o.get("holdSide") or trade_side or "").upper()
                closed.append({"pnl": pnl, "date": dt, "side": side})

            if closed:
                lines    = []
                net_pnl  = sum(t["pnl"] for t in closed)
                wins     = sum(1 for t in closed if t["pnl"] > 0)
                losses   = sum(1 for t in closed if t["pnl"] <= 0)
                net_sign = "🟢 +" if net_pnl >= 0 else "🔴 "

                for t in closed:
                    e = "🟢" if t["pnl"] >= 0 else "🔴"
                    s = "+" if t["pnl"] >= 0 else ""
                    lines.append(f"{e} {t['side']} closed {s}${t['pnl']:.2f} — {t['date']}")

                trades_section = (
                    f"\nTrades this week: {len(closed)} "
                    f"({wins}W / {losses}L)\n"
                    + "\n".join(lines)
                    + f"\n\nNet week: {net_sign}${abs(net_pnl):.2f}"
                )
            else:
                trades_section = "\nNo closed trades this week."

        except Exception as e:
            trades_section = f"\nTrade history unavailable: {str(e)[:80]}"

    # ── Text-only weekly recap (image module removed for memory) ───────────
    send_text(
        f"🤖 *ATRb v2 — Weekly Recap*\n"
        f"📅 {week_start_str} → {week_end_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{eth_line}"
        f"{trades_section}"
    )


def _job_monthly_perf():
    try:
        _send_monthly_perf()
    except Exception as e:
        _err("MonthlyPerf", e)


def _send_monthly_perf():
    """
    First Monday of each month at 09:30 — 30-day closed trade recap.
    Personal trading performance only — no strategy names or links.
    """
    from bot.datafeed_bitget import (
        _signed_request, _to_float,
        BITGET_PRODUCT_TYPE, BITGET_API_KEY
    )

    sym = os.getenv("INFINEX_SYMBOL", "ETHUSDT")
    now = datetime.now(timezone.utc)
    month_start_dt  = now - timedelta(days=30)
    month_start_str = month_start_dt.strftime("%b %d")
    month_end_str   = (now - timedelta(days=1)).strftime("%b %d")

    # ── ETH 30D price context ────────────────────────────────────────────────
    eth_line = ""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params={"symbol": sym, "granularity": "4H", "limit": "180",
                    "productType": BITGET_PRODUCT_TYPE},
            timeout=8,
        )
        data   = (r.json().get("data") or []) if r.ok else []
        closes = [float(row[4]) for row in data if isinstance(row, (list,tuple)) and len(row) >= 5]
        if closes:
            chg = (closes[-1] - closes[0]) / closes[0] * 100
            e   = "📈" if chg >= 0 else "📉"
            s   = "+" if chg >= 0 else ""
            eth_line = f"ETH/USDT: {e} {s}{chg:.1f}% this month\n"
    except Exception:
        pass

    # ── Closed trades (authenticated) ────────────────────────────────────────
    trades_section = ""
    if BITGET_API_KEY:
        try:
            start_ms = int(month_start_dt.timestamp() * 1000)
            end_ms   = int(now.timestamp() * 1000)

            res = _signed_request(
                "GET",
                "/api/v2/mix/order/history",
                params={
                    "symbol":      sym,
                    "productType": BITGET_PRODUCT_TYPE,
                    "startTime":   str(start_ms),
                    "endTime":     str(end_ms),
                    "limit":       "100",
                }
            )
            orders = ((res.get("data") or {}).get("orderList") or [])

            closed = []
            for o in orders:
                state      = (o.get("state") or "").lower()
                trade_side = (o.get("tradeSide") or o.get("side") or "").lower()
                pnl_raw    = o.get("pnl") or o.get("realizedPL") or o.get("profit") or ""
                if state != "filled":
                    continue
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue
                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue
                try:
                    ctime = int(o.get("cTime") or o.get("uTime") or 0)
                    dt    = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc).strftime("%b %d")
                except Exception:
                    dt = "—"
                side = (o.get("holdSide") or trade_side or "").upper()
                closed.append({"pnl": pnl, "date": dt, "side": side})

            if closed:
                net_pnl   = sum(t["pnl"] for t in closed)
                wins      = sum(1 for t in closed if t["pnl"] > 0)
                losses    = sum(1 for t in closed if t["pnl"] <= 0)
                best      = max(closed, key=lambda x: x["pnl"])
                worst     = min(closed, key=lambda x: x["pnl"])
                win_rate  = round(wins / len(closed) * 100)
                net_sign  = "📈 +" if net_pnl >= 0 else "📉 "

                trades_section = (
                    f"\nTrades: {len(closed)} ({wins}W / {losses}L) — {win_rate}% win rate\n"
                    f"Net PnL: {net_sign}${abs(net_pnl):.2f}\n"
                    f"Best:  📈 +${best['pnl']:.2f} — {best['date']}\n"
                    f"Worst: 📉 ${worst['pnl']:.2f} — {worst['date']}"
                )
            else:
                trades_section = "\nNo closed trades this month."

        except Exception as e:
            trades_section = f"\nTrade history unavailable: {str(e)[:80]}"

    send_text(
        f"🤖 *ATRb v2 — Monthly Recap*\n"
        f"📅 {month_start_str} → {month_end_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{eth_line}"
        f"{trades_section}"
    )


def _job_fedwatch_monday():
    """Monday 08:00 — push current rate probability to the group."""
    try:
        if hasattr(fedwatch, "show_rate_probability"):
            fedwatch.show_rate_probability()
        else:
            fedwatch.show_next_event()
    except Exception as e:
        _err("FedWatch Monday", e)


def _job_correlwatch():
    try:
        correlwatch.poll_once()
    except Exception as e:
        _err("CorrelWatch", e)

def _job_vixwatch():
    try:
        vixwatch.poll_once()
    except Exception as e:
        _err("VixWatch", e)


def _job_whalewatch():
    try:
        whalewatch.poll_once()
    except Exception as e:
        _err("WhaleWatch", e)


def _job_fundingwatch():
    try:
        fundingwatch.poll_once()
    except Exception as e:
        _err("FundingWatch", e)


def _job_oiwatch():
    try:
        oiwatch.poll_once()
    except Exception as e:
        _err("OIWatch", e)


def _job_optionswatch_thursday():
    try:
        optionswatch.run_thursday()
    except Exception as e:
        _err("OptionsWatch", e)


def _job_optionswatch_friday():
    try:
        optionswatch.run_friday()
    except Exception as e:
        _err("OptionsWatch", e)


def _job_optionswatch_refresh():
    """Silent STATE refresh for IntelWatch consumption."""
    try:
        optionswatch.refresh_state()
    except Exception as e:
        _err("OptionsWatch-refresh", e)


def _get_modules():
    """Bundle all module references for IntelWatch."""
    return {
        "fedwatch":        fedwatch,
        "trumpwatch":      trumpwatch_live,
        "vixwatch":        vixwatch,
        "correlwatch":     correlwatch,
        "fundingwatch":    fundingwatch,
        "oiwatch":         oiwatch,
        "optionswatch":    optionswatch,
    }



def _job_market_open():
    try:
        _send_market_open()
    except Exception as e:
        _err("MarketOpen", e)


def _send_market_open():
    """Mon–Fri 14:30 CET — US market open snapshot."""
    from bot.datafeed_bitget import BITGET_PRODUCT_TYPE

    def _ticker(sym):
        try:
            r = requests.get(
                "https://api.bitget.com/api/v2/mix/market/ticker",
                params={"symbol": sym, "productType": BITGET_PRODUCT_TYPE},
                timeout=6,
            )
            raw   = r.json() if r.ok else {}
            items = (raw or {}).get("data") or {}
            if isinstance(items, list):
                items = items[0] if items else {}
            def _f(k):
                v = items.get(k)
                try: return float(v) if v not in (None, "") else None
                except: return None
            return {
                "last":    _f("lastPr"),
                "chg24h":  _f("change24h"),
                "high":    _f("high24h"),
                "low":     _f("low24h"),
                "funding": _f("fundingRate"),
            }
        except Exception:
            return None

    btc = _ticker("BTCUSDT")
    eth = _ticker("ETHUSDT")
    now = datetime.now(timezone.utc)

    if not btc and not eth:
        return

    lines = [f"🔔 *US Market Open — {now.strftime('%b %d, %H:%M')} UTC*\n"]

    if btc:
        chg  = btc["chg24h"] or 0
        sign = "+" if chg >= 0 else ""
        e    = "📈" if chg >= 0 else "📉"
        fund = f" | Funding: {'positive' if (btc['funding'] or 0) > 0 else 'negative'}" if btc["funding"] is not None else ""
        lines.append(
            f"₿ BTC  ${btc['last']:,.0f} | {e} {sign}{chg:.2f}%"
            f" | H {btc['high']:,.0f} / L {btc['low']:,.0f}{fund}"
        )

    if eth:
        chg  = eth["chg24h"] or 0
        sign = "+" if chg >= 0 else ""
        e    = "📈" if chg >= 0 else "📉"
        fund = f" | Funding: {'positive' if (eth['funding'] or 0) > 0 else 'negative'}" if eth["funding"] is not None else ""
        # ETH vs BTC performance
        if btc and btc["chg24h"] is not None:
            rel = "outperformed" if chg > (btc["chg24h"] or 0) else "underperformed"
            lines.append(
                f"Ξ ETH  ${eth['last']:,.2f} | {e} {sign}{chg:.2f}%"
                f" | {rel} BTC{fund}"
            )
        else:
            lines.append(f"Ξ ETH  ${eth['last']:,.2f} | {e} {sign}{chg:.2f}%{fund}")

    # Overall bias
    btc_chg = (btc["chg24h"] or 0) if btc else 0
    eth_chg = (eth["chg24h"] or 0) if eth else 0
    avg_chg = (btc_chg + eth_chg) / 2

    if avg_chg > 1.5:
        bias = "🟢 Bullish open"
    elif avg_chg > 0:
        bias = "🟡 Cautious open"
    elif avg_chg > -1.5:
        bias = "🟠 Soft open"
    else:
        bias = "🔴 Bearish open"

    lines.append(f"\nBias: {bias}")

    send_text("\n".join(lines))


def _on_job_error(event):
    print(f"[APScheduler] Job {event.job_id} raised: {event.exception}", flush=True)

def start_scheduler():
    SCHED.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # ── TrumpWatch — 60s (was a raw thread with run_loop, now clean APScheduler)
    if os.getenv("ENABLE_TRUMPWATCH_LIVE", "true").lower() in ("1", "true", "yes", "on"):
        SCHED.add_job(
            _job_trumpwatch, "interval", seconds=60,
            id="trumpwatch", max_instances=1, misfire_grace_time=15,
        )
        print("🍊 TrumpWatch scheduled (60s) ✅", flush=True)

    # ── FedWatch — every 5 min (was a blocking schedule_loop thread)
    if os.getenv("ENABLE_FEDWATCH", "true").lower() in ("1", "true", "yes", "on"):
        SCHED.add_job(
            _job_fedwatch, "interval", minutes=5,
            id="fedwatch", max_instances=1, misfire_grace_time=60,
        )
        print("🏦 FedWatch scheduled (5min) ✅", flush=True)

    # ── MacroWatch Weekly Brief — Monday 09:00 (market report)
    SCHED.add_job(
        _job_weekly_brief, "cron", day_of_week="mon", hour=9, minute=0,
        id="weekly_brief", max_instances=1,
    )
    print("📊 MacroWatch Weekly Brief scheduled (Mon 09:00) ✅", flush=True)

    # ── Strategy Recap — Friday 09:00 (trades + positioning)
    SCHED.add_job(
        _job_strategy_recap, "cron", day_of_week="fri", hour=9, minute=0,
        id="strategy_recap", max_instances=1,
    )
    print("🤖 Strategy Recap scheduled (Fri 09:00) ✅", flush=True)

    # ── Weekly Intel Deep Dive — Wednesday 09:00
    SCHED.add_job(
        _job_weekly_intel, "cron", day_of_week="wed", hour=9, minute=0,
        id="weekly_intel", max_instances=1,
    )
    print("🧠 Weekly Intel Deep Dive scheduled (Wed 09:00) ✅", flush=True)

    # ── PositionWatch — every 10s
    SCHED.add_job(
        _job_positionwatch, "interval", seconds=10,
        id="positionwatch", max_instances=1, misfire_grace_time=5,
    )
    print("📘 PositionWatch scheduled (10s) ✅", flush=True)

    # ── Market Open Alert — Mon–Fri 13:30 UTC (14:30 CET)
    if os.getenv("ENABLE_MARKET_OPEN", "true").lower() in ("1", "true", "yes", "on"):
        SCHED.add_job(
            _job_market_open, "cron", day_of_week="mon-fri", hour=13, minute=30,
            id="market_open", max_instances=1,
        )
        print("🔔 Market Open Alert scheduled (Mon–Fri 13:30 UTC) ✅", flush=True)

    # ── CorrelWatch — every 30 minutes
    SCHED.add_job(
        _job_correlwatch, "interval", minutes=30,
        id="correlwatch", max_instances=1, misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=25),
    )
    print("📡 CorrelWatch scheduled (30min) ✅", flush=True)

    # ── VixWatch — every 30 minutes
    SCHED.add_job(
        _job_vixwatch, "interval", minutes=30,
        id="vixwatch", max_instances=1, misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=35),
    )
    print("😱 VixWatch scheduled (30min) ✅", flush=True)

    # ── WhaleWatch — every 5 min
    if os.getenv("ETHERSCAN_API_KEY"):
        SCHED.add_job(
            _job_whalewatch, "interval", minutes=5,
            id="whalewatch", max_instances=1, misfire_grace_time=30,
        )
        print("🐋 WhaleWatch scheduled (5min) ✅", flush=True)
    else:
        print("🐋 WhaleWatch disabled (ETHERSCAN_API_KEY not set)", flush=True)

    # ── Challenge update — Tuesday 09:15 (public channel)
    if os.getenv("PUBLIC_CHAT_ID"):
        SCHED.add_job(
            _job_challenge_update, "cron", day_of_week="tue", hour=9, minute=15,
            id="challenge_update", max_instances=1,
        )
        print("🎯 Challenge Update scheduled (Tue 09:15) ✅", flush=True)

    # ── FedWatch Monday rate probability push — Monday 08:00
    SCHED.add_job(
        _job_fedwatch_monday, "cron", day_of_week="mon", hour=8, minute=0,
        id="fedwatch_monday", max_instances=1,
    )
    print("🏦 FedWatch Monday push scheduled (Mon 08:00) ✅", flush=True)

    # ── FundingWatch — every 30 minutes
    SCHED.add_job(
        _job_fundingwatch, "interval", minutes=30,
        id="fundingwatch", max_instances=1, misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
    )
    print("💸 FundingWatch scheduled (30min) ✅", flush=True)

    # ── OIWatch — every 30 minutes
    SCHED.add_job(
        _job_oiwatch, "interval", minutes=30,
        id="oiwatch", max_instances=1, misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )
    print("📊 OIWatch scheduled (30min) ✅", flush=True)

    # ── OptionsWatch — Thursday 18:00 + Friday 07:00 UTC
    SCHED.add_job(
        _job_optionswatch_thursday, "cron", day_of_week="thu", hour=18, minute=0,
        id="optionswatch_thursday", max_instances=1,
    )
    SCHED.add_job(
        _job_optionswatch_friday, "cron", day_of_week="fri", hour=7, minute=0,
        id="optionswatch_friday", max_instances=1,
    )
    print("⚙️ OptionsWatch scheduled (Thu 18:00 + Fri 07:00 UTC) ✅", flush=True)

    # ── OptionsWatch STATE refresh — every 30 min (silent, feeds IntelWatch)
    SCHED.add_job(
        _job_optionswatch_refresh, "interval", minutes=30,
        id="optionswatch_refresh", max_instances=1, misfire_grace_time=60,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    print("⚙️ OptionsWatch state refresh scheduled (30min silent) ✅", flush=True)

    # ── MarketStructure — 4H close + 2 min (BTC + ETH, 1 min apart)
    # Bitget's 4H candles close at 00/04/08/12/16/20:00 UTC. The scheduler's
    # default timezone is Europe/Brussels (correct for human-facing reports
    # like the Monday brief / Friday recap), so this job needs an explicit
    # UTC override or it fires 2h early (Brussels = UTC+2 in summer, UTC+1
    # in winter) and reads a still-forming candle.
    SCHED.add_job(
        market_structure.poll_all, "cron",
        hour="0,4,8,12,16,20", minute=2, timezone="UTC",
        id="market_structure", max_instances=1, misfire_grace_time=300,
    )
    print("📊 MarketStructure scheduled (4H close +2min UTC) ✅", flush=True)

    # ── Gate auto-propose scan (intrabar with-trend GO → stage card) ──
    if hasattr(gatewatch, "scan"):
        SCHED.add_job(
            gatewatch.scan, "interval", minutes=3,
            id="gate_scan", max_instances=1, misfire_grace_time=120,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
        )
        print("⚡ Gate auto-propose scan scheduled (3 min) ✅", flush=True)
    else:
        print("⚠️ gatewatch.scan missing — auto-propose scan NOT scheduled", flush=True)

    SCHED.start()
    print("🕒 APScheduler started ✅", flush=True)
    print("🤖 StratWatch ready — /status command live ✅", flush=True)
    print("💸 FundingWatch · 📊 OIWatch · ⚙️ OptionsWatch ready ✅", flush=True)
    print("🧠 IntelWatch ready ✅", flush=True)
    print("😱 VixWatch ready — /vix command live ✅", flush=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_uptime() -> str:
    delta = datetime.now(timezone.utc) - STARTED_AT_UTC
    h, rem = divmod(int(delta.total_seconds()), 3600)
    return f"{h:02d}:{rem // 60:02d}:{rem % 60:02d}"

def _compute_levels_from_candles(candles, lookback=48):
    if not candles:
        return None
    lb = candles[-lookback:] if len(candles) > lookback else candles
    return {
        "support":    min(c["low"]   for c in lb),
        "resistance": max(c["high"]  for c in lb),
        "last":       candles[-1]["close"],
    }

def _atr_simple(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = [
        max(candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i-1]["close"]),
            abs(candles[i]["low"]  - candles[i-1]["close"]))
        for i in range(-period, 0)
    ]
    return sum(trs) / len(trs) if trs else 0.0




# ─── Health summary ──────────────────────────────────────────────────────────

def _build_health_msg() -> str:
    lines = [
        "🩺 *MacroWatch Health*",
        f"⏱ Uptime: {_fmt_uptime()}",
        f"🕐 Started: {STARTED_AT_UTC.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"🐍 Python {sys.version.split()[0]} | {platform.system()} {platform.release()}",
        "",
        "📅 *Scheduler Jobs*",
    ]

    for job in SCHED.get_jobs():
        next_run = job.next_run_time
        next_str = next_run.strftime("%H:%M:%S UTC") if next_run else "paused"
        lines.append(f"  ✅ `{job.id}` — next: {next_str}")

    # TrumpWatch state
    tw_seen  = len(trumpwatch_live.STATE.get("seen", {}))
    tw_health = trumpwatch_live.STATE.get("source_health", {})
    tw_src_ok = all(h.get("ok") for h in tw_health.values()) if tw_health else False
    lines += [
        "",
        "🍊 *TrumpWatch*",
        f"  Sources: {'✅ All OK' if tw_src_ok else '⚠️ Degraded'}",
        f"  Dedup cache: {tw_seen} entries",
        f"  Buffered alerts: {len(trumpwatch_live.RECENT_ALERTS)}",
    ]

    # WhaleWatch state
    ww_check = whalewatch.STATE.get("last_check_utc")
    ww_fired = whalewatch.STATE.get("total_fired", 0)
    ww_last  = whalewatch.STATE.get("last_alert_utc")
    ww_key   = "✅ Configured" if os.getenv("ETHERSCAN_API_KEY") else "❌ ETHERSCAN_API_KEY not set"
    lines += [
        "",
        "🐋 *WhaleWatch*",
        f"  API: {ww_key}",
        f"  Last check: {ww_check.strftime('%H:%M UTC') if ww_check else '—'}",
        f"  Alerts fired: {ww_fired}",
        f"  Last alert: {ww_last.strftime('%Y-%m-%d %H:%M UTC') if ww_last else 'None yet'}",
    ]

    # CorrelWatch state
    cw_last  = correlwatch.STATE.get("last_check_utc")
    cw_dxy   = correlwatch.STATE.get("last_dxy")
    cw_btc   = correlwatch.STATE.get("last_btc")
    cw_alert = correlwatch.STATE.get("last_alert_utc")
    lines += [
        "",
        "📡 *CorrelWatch*",
        f"  Last check: {cw_last.strftime('%H:%M UTC') if cw_last else '—'}",
        f"  DXY: {f'{cw_dxy:+.2f}%' if cw_dxy is not None else '—'} | BTC: {f'{cw_btc:+.2f}%' if cw_btc is not None else '—'}",
        f"  Last alert: {cw_alert.strftime('%Y-%m-%d %H:%M UTC') if cw_alert else 'None yet'}",
    ]

    # FedWatch state
    fw_events = len(fedwatch.STATE.get("events", []))
    fw_alerts = len(fedwatch.STATE.get("alert_queue", []))
    lines += [
        "",
        "🏦 *FedWatch*",
        f"  Events loaded: {fw_events}",
        f"  Queued alerts: {fw_alerts}",
        f"  Source: {'✅ OK' if fedwatch.STATE.get('source_ok') else '⚠️ Degraded'}",
    ]

    # FundingWatch state
    fw_check = fundingwatch.STATE.get("last_check")
    fw_rates = fundingwatch.STATE.get("last_rates", {})
    lines += [
        "",
        "💸 *FundingWatch*",
        f"  Last check: {fw_check.strftime('%H:%M UTC') if fw_check else '—'}",
        f"  BTC: {fw_rates.get('BTCUSDT', '—')}%  ETH: {fw_rates.get('ETHUSDT', '—')}%",
    ]

    # OIWatch state
    oi_check = oiwatch.STATE.get("last_check")
    oi_data  = oiwatch.STATE.get("last_oi", {})
    btc_oi   = '${:.2f}B'.format(oi_data['BTCUSDT']/1e9) if oi_data.get('BTCUSDT') else '—'
    eth_oi   = '${:.2f}B'.format(oi_data['ETHUSDT']/1e9) if oi_data.get('ETHUSDT') else '—'
    lines += [
        "",
        "📊 *OIWatch*",
        f"  Last check: {oi_check.strftime('%H:%M UTC') if oi_check else '—'}",
        f"  BTC OI: {btc_oi}  |  ETH OI: {eth_oi}",
    ]

    # OptionsWatch state (BTC + ETH)
    opt_last  = optionswatch.STATE.get("last_alert_utc")
    btc_state = optionswatch.STATE.get("btc", {}) or {}
    eth_state = optionswatch.STATE.get("eth", {}) or {}
    btc_pain  = btc_state.get("max_pain")
    eth_pain  = eth_state.get("max_pain")
    btc_exp   = btc_state.get("expiry_str") or "—"
    eth_exp   = eth_state.get("expiry_str") or "—"
    lines += [
        "",
        "⚙️ *OptionsWatch*",
        f"  Last alert: {opt_last.strftime('%Y-%m-%d %H:%M UTC') if opt_last else '—'}",
        f"  BTC: {btc_exp}  Max pain: {'${:,.0f}'.format(btc_pain) if btc_pain else '—'}",
        f"  ETH: {eth_exp}  Max pain: {'${:,.0f}'.format(eth_pain) if eth_pain else '—'}",
    ]

    return "\n".join(lines)


# ─── Command loop ─────────────────────────────────────────────────────────────

def command_loop():
    offset     = None
    chat_allow = str(os.getenv("CHAT_ID") or "")

    while True:
        try:
            data = get_updates(offset=offset, timeout=20)
            for upd in data.get("result", []):
                offset   = upd["update_id"] + 1

                # ── Button tap (approve/skip) ────────────────────────
                cb = upd.get("callback_query")
                if cb:
                    if chat_allow:
                        cb_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
                        if cb_chat and cb_chat != chat_allow:
                            continue
                    try:
                        stagewatch.handle_callback(cb)
                    except Exception as e:
                        print(f"[callback] {e}", flush=True)
                    continue

                msg      = upd.get("message") or {}
                text_raw = (msg.get("text") or "").strip()
                if not text_raw:
                    continue
                text = text_raw.lower()
                chat = str((msg.get("chat") or {}).get("id") or "")
                if chat_allow and chat != chat_allow:
                    continue

                # ── New member welcome ───────────────────────────────
                new_members = msg.get("new_chat_members") or []
                for member in new_members:
                    if member.get("is_bot"):
                        continue
                    username = member.get("username")
                    first    = member.get("first_name", "")
                    mention  = f"@{username}" if username else first
                    send_text(
                        f"🤖 Welcome {mention}!\n\n"
                        f"You just joined Infinex Capital HQ —\n"
                        f"home of ATRb v2, a fully automated trading strategy\n"
                        f"running 24/7 on ETH (4H timeframe).\n\n"
                        f"No charts. No noise. No emotion.\n"
                        f"Just the system doing its work.\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Here's what to do:\n\n"
                        f"1. Read the pinned message\n"
                        f"2. Type /status to see the strategy live\n"
                        f"3. Type /bot_challenge for the ATRb v2 $1k → $100k journey\n"
                        f"4. Activate copy trading on Bitget to mirror every trade\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🚀 Copy trading live May 1, 2026\n"
                        f"https://www.bitget.com/copy-trading/futures-trader-v1/bcb7467487b53c5fa395?clacCode=4Y4MLFF1"
                    )

                if not text:
                    continue

                try:
                    _handle_command(text, text_raw)
                except Exception as e:
                    send_text(f"⚠️ Command error: {str(e)[:200]}")

        except Exception as e:
            # Never let the command loop die — log and keep going
            print(f"[command_loop] Error: {e}", flush=True)
            time.sleep(5)


def _handle_command(text: str, text_raw: str):

    # ── /scandiag ─ what the auto-propose scan sees right now ─────────────────
    if text.startswith("/scandiag"):
        try:
            parts = text.split()
            gatewatch.scan_report(parts[1] if len(parts) > 1 else "BTCUSDT")
        except Exception as e:
            send_text(f"🔍 [Scandiag] {e}")
        return

    # ── /gate ─ on-demand gate read ───────────────────────────────────────────
    if text.startswith("/gate"):
        try:
            parts = text.split()
            gatewatch.run_gate(parts[1] if len(parts) > 1 else "BTCUSDT")
        except Exception as e:
            send_text(f"🎯 [Gate] {e}")
        return

    # ── /stage ─ manually stage a plan with approve buttons ───────────────────
    if text.startswith("/stage"):
        try:
            stagewatch.stage(text_raw)
        except Exception as e:
            send_text(f"📋 [Stage] {e}")
        return

    # ── /flatten ─ kill switch ────────────────────────────────────────────────
    if text.startswith("/flatten"):
        try:
            parts = text_raw.split()
            stagewatch.flatten_cmd(parts[1] if len(parts) > 1 else "")
        except Exception as e:
            send_text(f"🛑 [Stage] {e}")
        return

    # ── /help ────────────────────────────────────────────────────────────────
    if text.startswith("/help"):
        send_text(
            "🤖 *MacroWatch — Command Guide*\n\n"
            "🍊 *TrumpWatch*\n"
            "`/trumpwatch` — Trigger immediate live poll\n"
            "`/tw_recent` — Last 10 alerts\n"
            "`/tw_diag` — Source health + dedup stats\n"
            "`/tw_clear` — Clear dedup cache (re-enables old posts)\n\n"
            "🏦 *FedWatch*\n"
            "`/fedwatch` — Next Fed event\n"
            "`/fed_diag` — Calendar + rate probability\n\n"
            "📊 *MacroWatch Weekly*\n"
            "`/weekly` — Full weekly market brief\n\n"
            "📡 *CorrelWatch*\n"
            "`/correl_diag` — DXY vs BTC last reading\n\n"
            "💸 *FundingWatch*\n"
            "`/funding_diag` — Current funding rates\n\n"
            "📊 *OIWatch*\n"
            "`/oi_diag` — Current open interest\n\n"
            "⚙️ *OptionsWatch*\n"
            "`/options_diag` — Last expiry analysis\n"
            "`/options_now` — Run analysis now\n\n"
            "🧠 *IntelWatch*\n"
            "`/intel` — Full market intelligence briefing\n\n"
            "😱 *VixWatch*\n"
            "`/vix` — Current VIX reading + market context\n"
            "`/vix_diag` — Last value + alert state\n\n"
            "🩺 *System*\n"
            "`/health` — Full system status\n"
            "`/restart` — Trigger clean poll of all modules\n"
            "`/status` — ATRb v2 live strategy status (indicators + regime)\n"
            "`/bot` — ATRb v2 current open position(s)\n"
            "`/live` — TraderWatch current open position(s)\n"
            "`/structure` — 4H S/R + regime + funding/OI (BTC or ETH)\n"
            "`/cvd_log` — last 10 CVD trigger verdicts (add a number for more)\n"
            "`/bot_challenge` — ATRb v2 $1k → $100k progress\n"
            "`/live_challenge` — TraderWatch $1k → $10k progress\n"
            "`/report` — Last 7 days trades + P&L\n"
            "`/plan` — Post enriched plan for the open position (R:R, risk %, liq, ratchet)\n"
        )
        return

    # ── /health ───────────────────────────────────────────────────────────────
    if text.startswith("/health"):
        send_text(_build_health_msg())
        return

    # ── /restart ──────────────────────────────────────────────────────────────
    if text.startswith("/restart"):
        send_text("🔄 Triggering clean poll of all modules...")
        results = []
        for label, fn in [
            ("TrumpWatch", _job_trumpwatch),
            ("FedWatch",   _job_fedwatch),
        ]:
            try:
                fn()
                results.append(f"✅ {label}")
            except Exception as e:
                results.append(f"❌ {label}: {str(e)[:80]}")
        send_text("🔄 Poll complete:\n" + "\n".join(results))
        return

    # ── /tw_diag ──────────────────────────────────────────────────────────────
    if text.startswith("/tw_diag"):
        try:
            trumpwatch_live.run_diag()
        except Exception as e:
            send_text(f"🍊 [TrumpWatch] Diag error: {e}")
        return

    # ── /tw_clear ─────────────────────────────────────────────────────────────
    if text.startswith("/tw_clear"):
        try:
            mem_count   = len(trumpwatch_live.STATE["seen"])
            trumpwatch_live.STATE["seen"].clear()
            redis_count = trumpwatch_live._redis_flush()
            total       = mem_count + redis_count
            send_text(
                f"🍊 [TrumpWatch] Dedup cache cleared\n"
                f"• Memory: {mem_count} entries removed\n"
                f"• Redis: {redis_count} keys deleted\n"
                f"Next poll will re-evaluate all recent posts."
            )
        except Exception as e:
            send_text(f"🍊 [TrumpWatch] Clear error: {e}")
        return

    # ── /trumpwatch ───────────────────────────────────────────────────────────
    if text.startswith("/trumpwatch"):
        try:
            trumpwatch_live.poll_once()
            send_text("🍊 [TrumpWatch] Live poll executed.")
        except Exception as e:
            send_text(f"🍊 [TrumpWatch] Poll error: {e}")
        return

    if text.startswith("/tw_recent"):
        try:
            trumpwatch_live.show_recent()
        except Exception as e:
            send_text(f"🍊 [TrumpWatch] Recent error: {e}")
        return

    if text.startswith("/tw_sentiment"):
        try:
            trumpwatch_live.show_sentiment()
        except Exception as e:
            send_text(f"🍊 [TrumpWatch] Sentiment error: {e}")
        return

    # ── /fedwatch ─────────────────────────────────────────────────────────────
    if text.startswith("/fedwatch"):
        fedwatch.show_next_event()
        return

    if text.startswith("/fed_diag"):
        fedwatch.show_diag()
        return

    # ── /weekly ───────────────────────────────────────────────────────────────
    if text.startswith("/weekly"):
        try:
            send_text("📊 Building weekly brief — takes ~15s...")
            from bot.modules.weeklybrief import send_weekly_brief
            send_weekly_brief(_get_modules())
        except Exception as e:
            send_text(f"📊 [WeeklyBrief] Error: {e}")
        return

    # ── /correl_diag ─────────────────────────────────────────────────────────
    if text.startswith("/correl_diag"):
        try:
            correlwatch.show_diag()
        except Exception as e:
            send_text(f"📡 [CorrelWatch] Diag error: {e}")
        return

    # ── /funding_diag ─────────────────────────────────────────────────────────
    if text.startswith("/funding_diag"):
        try:
            fundingwatch.show_diag()
        except Exception as e:
            send_text(f"💸 [FundingWatch] Diag error: {e}")
        return

    # ── /oi_diag ──────────────────────────────────────────────────────────────
    if text.startswith("/oi_diag"):
        try:
            oiwatch.show_diag()
        except Exception as e:
            send_text(f"📊 [OIWatch] Diag error: {e}")
        return

    # ── /options_diag / /options_now ─────────────────────────────────────────
    if text.startswith("/options_now"):
        try:
            send_text("⚙️ Running options analysis — takes ~30s...")
            optionswatch.run_thursday()
        except Exception as e:
            send_text(f"⚙️ [OptionsWatch] Error: {e}")
        return

    if text.startswith("/options_diag"):
        try:
            optionswatch.show_diag()
        except Exception as e:
            send_text(f"⚙️ [OptionsWatch] Diag error: {e}")
        return

    # ── /intel ────────────────────────────────────────────────────────────────
    if text.startswith("/intel"):
        try:
            send_text("🧠 Compiling full market briefing...")
            intelwatch.show_intel(_get_modules())
        except Exception as e:
            send_text(f"🧠 [IntelWatch] Error: {e}")
        return

    # ── /vix / /vix_diag ─────────────────────────────────────────────────────
    if text.startswith("/vix_diag"):
        try:
            vixwatch.show_diag()
        except Exception as e:
            send_text(f"😱 [VixWatch] Diag error: {e}")
        return

    if text.startswith("/vix"):
        try:
            vixwatch.show_vix()
        except Exception as e:
            send_text(f"😱 [VixWatch] Error: {e}")
        return

    # ── /status ───────────────────────────────────────────────────────────────
    if text.startswith("/status"):
        try:
            send_text("🤖 Fetching strategy status...")
            stratwatch.show_status()
        except Exception as e:
            send_text(f"🤖 [StratWatch] Error: {e}")
        return

    # ── /challenge_diag (must come first - longest prefix wins) ──────────────
    if text.startswith("/challenge_diag"):
        try:
            challengewatch.show_challenge_diag()
        except Exception as e:
            send_text(f"🎯 [Diag] Error: {e}")
        return

    # ── /bot_challenge → ATRb v2 systematic ($1k → $100k) ─────────────────────
    if text.startswith("/bot_challenge"):
        try:
            challengewatch.show_bot_challenge()
        except Exception as e:
            send_text(f"🤖 [Bot Challenge] Error: {e}")
        return

    # ── /live_challenge → TraderWatch discretionary ($1k → $10k) ───────────────────
    if text.startswith("/live_challenge"):
        try:
            challengewatch.show_live_challenge()
        except Exception as e:
            send_text(f"🎯 [Live Challenge] Error: {e}")
        return

    # ── /challenge (legacy alias) → defaults to LIVE challenge ───────────────
    if text.startswith("/challenge"):
        try:
            challengewatch.show_challenge()
        except Exception as e:
            send_text(f"🎯 [Challenge] Error: {e}")
        return

    # ── /report ───────────────────────────────────────────────────────────────
    if text.startswith("/report"):
        try:
            reportwatch.show_report()
        except Exception as e:
            send_text(f"🤖 [Report] Error: {e}")
        return

    # ── /bot → ATRb v2 systematic live state ──────────────────────────────────
    if text.startswith("/bot") and not text.startswith("/bot_challenge"):
        try:
            tradewatch.show_bot_state()
        except Exception as e:
            send_text(f"🤖 [Bot State] Error: {e}")
        return

    # ── /live → TraderWatch discretionary live state ──────────────────────────
    if text.startswith("/live") and not text.startswith("/live_challenge"):
        try:
            tradewatch.show_live_state()
        except Exception as e:
            send_text(f"🎯 [Live State] Error: {e}")
        return

    # ── /structure [BTC|ETH] — live 4H S/R + regime + funding/OI ─────────────
    if text.startswith("/structure"):
        try:
            parts = text.split()
            sym = parts[1].upper() if len(parts) > 1 else "BTC"
            market_structure.show_structure(sym)
        except Exception as e:
            send_text(f"📊 [MarketStructure] Error: {e}")
        return

    # ── /cvd_log [N] — last N CVD trigger verdicts ────────────────────────────
    if text.startswith("/cvd_log"):
        try:
            parts = text.split()
            n = int(parts[1]) if len(parts) > 1 else 10
            market_structure.show_cvd_log(n)
        except Exception as e:
            send_text(f"📊 [CVD Log] Error: {e}")
        return

    # ── /plan ─────────────────────────────────────────────────────────────────
    if text.startswith("/plan"):
        try:
            parts = text.split()
            sym = parts[1].upper() if len(parts) > 1 else ""
            tradewatch.show_plan(sym)
        except Exception as e:
            send_text(f"📋 [TradeWatch] Error: {e}")
        return


# ─── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
    threading.Thread(target=command_loop, daemon=True).start()
    print("💬 Command loop started ✅", flush=True)

    # Keep process alive
    while True:
        time.sleep(3600)