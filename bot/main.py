# bot/main.py

“””
MacroWatch — Entry point

Polling architecture (all via APScheduler, no raw threads for polling):
TrumpWatch   → every 60s
FedWatch     → every 5min
CryptoWatch  → weekly cron (Sunday 18:00)
CryptoDaily  → daily cron  (15:28)
PositionWatch → every 10s (open/close/TP/SL detection)

Command loop runs in a single daemon thread.
All poll functions are wrapped so one crash never kills the scheduler.
“””

import os
import sys
import json
import platform
import threading
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

from bot.utils import send_text, get_updates

import bot.modules.fedwatch        as fedwatch
import bot.modules.cryptowatch     as cryptowatch
import bot.modules.cryptowatch_daily as cryptowatch_daily
import bot.modules.trumpwatch_live as trumpwatch_live

STARTED_AT_UTC = datetime.now(timezone.utc)

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

_POS_SNAPSHOT: dict = {}   # { “BTCUSDT”: { has_position, side, size, entry, tp, sl }, … }
_POS_INITIALISED = False

# ─── Scheduler (module-level so commands can inspect jobs) ───────────────────

SCHED = BackgroundScheduler(timezone=os.getenv(“TIMEZONE”, “Europe/Brussels”))

# ─── Safe job wrappers ───────────────────────────────────────────────────────

# Each wrapper catches its own errors so one broken module never kills others.

def _job_trumpwatch():
try:
trumpwatch_live.poll_once()
except Exception as e:
_err(“TrumpWatch”, e)

def _job_fedwatch():
try:
fedwatch.poll_once()
except Exception as e:
_err(“FedWatch”, e)

def _job_cryptowatch_daily():
try:
if hasattr(cryptowatch_daily, “main”):
cryptowatch_daily.main()
except Exception as e:
_err(“CryptoWatch Daily”, e)

def _job_cryptowatch_weekly():
try:
if hasattr(cryptowatch, “main”):
cryptowatch.main()
except Exception as e:
_err(“CryptoWatch Weekly”, e)

def _err(module: str, exc: Exception):
msg = f”⚠️ [{module}] Job error: {str(exc)[:200]}”
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
_err(“PositionWatch”, e)

def _poll_positions():
global _POS_INITIALISED, _POS_SNAPSHOT

```
symbols = BITGET_SYMBOLS or ["BTCUSDT", "ETHUSDT"]

for sym in symbols:
    sym = sym.strip().upper()
    try:
        pos    = _fetch_current_futures_position(sym)
        orders = _fetch_pending_tp_sl_orders(sym)
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

        prev = _POS_SNAPSHOT.get(sym)

        # First run — seed baseline silently
        if not _POS_INITIALISED:
            _POS_SNAPSHOT[sym] = cur
            continue

        if prev is None:
            _POS_SNAPSHOT[sym] = cur
            continue

        # ── Detect changes ───────────────────────────────────────────
        side_emoji = "🟢" if cur["side"] == "LONG" else "🔴"

        # Position opened
        if not prev["has_position"] and cur["has_position"]:
            cur["opened_at"] = datetime.now(timezone.utc)
            send_text(
                f"📘 *Position Opened*\n"
                f"Pair: {sym}\n"
                f"Side: {side_emoji} {cur['side']}\n"
                f"Entry: {cur['entry']:.2f}\n"
                f"Size: {cur['size']}\n"
                f"Leverage: {cur['lev']}x\n"
                f"Time (UTC): {iso_utc_now()}"
            )

        # Position closed
        elif prev["has_position"] and not cur["has_position"]:
            prev_side = prev.get("side") or "?"
            prev_emoji = "🟢" if prev_side == "LONG" else "🔴"
            entry = prev.get("entry", 0.0)

            # Fetch last price for PnL estimate
            try:
                from bot.datafeed_bitget import get_ticker
                last_px = get_ticker(sym) or 0.0
            except Exception:
                last_px = 0.0

            # PnL % from entry to close price
            pnl_pct = ""
            pnl_sign = ""
            if entry and last_px:
                raw = (last_px - entry) / entry * 100
                if prev_side == "SHORT":
                    raw = -raw
                lev = _to_float(prev.get("lev") or 1)
                leveraged = raw * lev
                sign = "🟢 +" if leveraged >= 0 else "🔴 "
                pnl_pct = f"\nEst. PnL: {sign}{leveraged:.1f}% (@ {last_px:.2f})"

            # Hold duration
            duration = ""
            opened_at = prev.get("opened_at")
            if opened_at:
                delta = datetime.now(timezone.utc) - opened_at
                h, rem = divmod(int(delta.total_seconds()), 3600)
                m = rem // 60
                duration = f"\nHeld: {h}h {m:02d}m"

            send_text(
                f"🏁 *Position Closed*\n"
                f"Pair: {sym}\n"
                f"Side: {prev_emoji} {prev_side}"
                f"{pnl_pct}"
                f"{duration}\n"
                f"Time (UTC): {iso_utc_now()}"
            )

        elif cur["has_position"] and prev["has_position"]:
            prev_tps = prev.get("tp") or []
            cur_tps  = cur.get("tp") or []
            prev_sls = prev.get("sl") or []
            cur_sls  = cur.get("sl") or []

            # TP hit — a TP price disappeared from the order list
            for tp in prev_tps:
                if tp not in cur_tps:
                    send_text(
                        f"✅ *TP Hit*\n"
                        f"Pair: {sym}\n"
                        f"Side: {side_emoji} {cur['side']}\n"
                        f"TP: {tp}\n"
                        f"Time (UTC): {iso_utc_now()}"
                    )

            # SL hit — SL price disappeared and position still open (partial fill)
            # or position closed handles full SL — detect via SL disappearing
            for sl in prev_sls:
                if sl not in cur_sls and not cur["has_position"]:
                    send_text(
                        f"❌ *SL Hit*\n"
                        f"Pair: {sym}\n"
                        f"Side was: {side_emoji} {prev['side']}\n"
                        f"SL: {sl}\n"
                        f"Time (UTC): {iso_utc_now()}"
                    )

        _POS_SNAPSHOT[sym] = cur

    except Exception as e:
        print(f"[PositionWatch] Error for {sym}: {e}", flush=True)

# Mark initialised after first full pass
if not _POS_INITIALISED:
    _POS_INITIALISED = True
    print("📘 PositionWatch baseline set ✅", flush=True)
```

# ─── WeeklyPerf ──────────────────────────────────────────────────────────────

def _job_weekly_perf():
try:
_send_weekly_perf()
except Exception as e:
_err(“WeeklyPerf”, e)

def _send_weekly_perf():
“””
Monday 09:00 — INFINEX weekly performance recap.
Fetches ETH 7-day OHLC from Bitget 4H candles and posts a clean summary.
“””
from bot.datafeed_bitget import _public_get, BITGET_PRODUCT_TYPE
import json as _json

```
sym = os.getenv("INFINEX_SYMBOL", "ETHUSDT")

try:
    raw = _public_get(
        "/api/v2/mix/market/candles",
        {"symbol": sym, "granularity": "4H", "limit": "42",
         "productType": BITGET_PRODUCT_TYPE}
    )
    data = (raw or {}).get("data") or []
    closes = [float(r[4]) for r in data if isinstance(r, (list,tuple)) and len(r) >= 5]
    highs  = [float(r[2]) for r in data if isinstance(r, (list,tuple)) and len(r) >= 5]
    lows   = [float(r[3]) for r in data if isinstance(r, (list,tuple)) and len(r) >= 5]
except Exception as e:
    send_text(f"📊 [WeeklyPerf] Could not fetch ETH data: {e}")
    return

if not closes:
    send_text("📊 [WeeklyPerf] No candle data available.")
    return

open_px  = closes[0]
close_px = closes[-1]
high_px  = max(highs)
low_px   = min(lows)
chg_pct  = (close_px - open_px) / open_px * 100

chg_emoji = "🟢" if chg_pct >= 0 else "🔴"
sign      = "+" if chg_pct >= 0 else ""

now = datetime.now(timezone.utc)
week_end   = (now - timedelta(days=1)).strftime("%b %d")
week_start = (now - timedelta(days=7)).strftime("%b %d")

send_text(
    f"📊 *INFINEX Weekly Performance*\n"
    f"Week: {week_start} → {week_end}\n\n"
    f"ETH/USDT — 7D\n"
    f"{chg_emoji} Change: {sign}{chg_pct:.1f}%\n"
    f"Open:  ${open_px:,.2f}\n"
    f"Close: ${close_px:,.2f}\n"
    f"High:  ${high_px:,.2f}\n"
    f"Low:   ${low_px:,.2f}\n\n"
    f"Strategy: INFINEX ETH 3H — Sentinel V2 & Ascent V2\n"
    f"Copy trading: https://share.glassgs.com/sl/H44FZLYY60X3"
)
```

def _job_fedwatch_monday():
“”“Monday 08:00 — push current rate probability to the group.”””
try:
if hasattr(fedwatch, “show_rate_probability”):
fedwatch.show_rate_probability()
else:
fedwatch.show_next_event()
except Exception as e:
_err(“FedWatch Monday”, e)

def _on_job_error(event):
print(f”[APScheduler] Job {event.job_id} raised: {event.exception}”, flush=True)

def start_scheduler():
SCHED.add_listener(_on_job_error, EVENT_JOB_ERROR)

```
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

# ── CryptoWatch Daily — 13:00 weekdays only (Mon–Fri)
if os.getenv("ENABLE_CRYPTOWATCH_DAILY", "true").lower() in ("1", "true", "yes", "on"):
    SCHED.add_job(
        _job_cryptowatch_daily, "cron", day_of_week="mon-fri", hour=13, minute=0,
        id="cryptowatch_daily", max_instances=1,
    )
    print("📊 CryptoWatch Daily scheduled (Mon–Fri 13:00) ✅", flush=True)

# ── CryptoWatch Weekly — Sunday 18:00
if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() in ("1", "true", "yes", "on"):
    SCHED.add_job(
        _job_cryptowatch_weekly, "cron", day_of_week="sun", hour=18, minute=0,
        id="cryptowatch_weekly", max_instances=1,
    )
    print("📊 CryptoWatch Weekly scheduled (Sun 18:00) ✅", flush=True)

# ── PositionWatch — every 10s
SCHED.add_job(
    _job_positionwatch, "interval", seconds=10,
    id="positionwatch", max_instances=1, misfire_grace_time=5,
)
print("📘 PositionWatch scheduled (10s) ✅", flush=True)

# ── WeeklyPerf — Monday 09:00
SCHED.add_job(
    _job_weekly_perf, "cron", day_of_week="mon", hour=9, minute=0,
    id="weekly_perf", max_instances=1,
)
print("📊 WeeklyPerf scheduled (Mon 09:00) ✅", flush=True)

# ── FedWatch Monday rate probability push — Monday 08:00
SCHED.add_job(
    _job_fedwatch_monday, "cron", day_of_week="mon", hour=8, minute=0,
    id="fedwatch_monday", max_instances=1,
)
print("🏦 FedWatch Monday push scheduled (Mon 08:00) ✅", flush=True)

SCHED.start()
print("🕒 APScheduler started ✅", flush=True)
```

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_uptime() -> str:
delta = datetime.now(timezone.utc) - STARTED_AT_UTC
h, rem = divmod(int(delta.total_seconds()), 3600)
return f”{h:02d}:{rem // 60:02d}:{rem % 60:02d}”

def _compute_levels_from_candles(candles, lookback=48):
if not candles:
return None
lb = candles[-lookback:] if len(candles) > lookback else candles
return {
“support”:    min(c[“low”]   for c in lb),
“resistance”: max(c[“high”]  for c in lb),
“last”:       candles[-1][“close”],
}

def _atr_simple(candles, period=14):
if len(candles) < period + 1:
return 0.0
trs = [
max(candles[i][“high”] - candles[i][“low”],
abs(candles[i][“high”] - candles[i-1][“close”]),
abs(candles[i][“low”]  - candles[i-1][“close”]))
for i in range(-period, 0)
]
return sum(trs) / len(trs) if trs else 0.0

# ─── Health summary ──────────────────────────────────────────────────────────

def _build_health_msg() -> str:
lines = [
“🩺 *MacroWatch Health*”,
f”⏱ Uptime: {_fmt_uptime()}”,
f”🕐 Started: {STARTED_AT_UTC.strftime(’%Y-%m-%d %H:%M:%S’)} UTC”,
f”🐍 Python {sys.version.split()[0]} | {platform.system()} {platform.release()}”,
“”,
“📅 *Scheduler Jobs*”,
]

```
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

return "\n".join(lines)
```

# ─── Command loop ─────────────────────────────────────────────────────────────

def command_loop():
offset     = None
chat_allow = str(os.getenv(“CHAT_ID”) or “”)

```
while True:
    try:
        data = get_updates(offset=offset, timeout=20)
        for upd in data.get("result", []):
            offset   = upd["update_id"] + 1
            msg      = upd.get("message") or {}
            text_raw = (msg.get("text") or "").strip()
            if not text_raw:
                continue
            text = text_raw.lower()
            chat = str((msg.get("chat") or {}).get("id") or "")
            if chat_allow and chat != chat_allow:
                continue

            try:
                _handle_command(text, text_raw)
            except Exception as e:
                send_text(f"⚠️ Command error: {str(e)[:200]}")

    except Exception as e:
        # Never let the command loop die — log and keep going
        print(f"[command_loop] Error: {e}", flush=True)
        time.sleep(5)
```

def _handle_command(text: str, text_raw: str):

```
# ── /help ────────────────────────────────────────────────────────────────
if text.startswith("/help"):
    send_text(
        "🤖 *MacroWatch — Command Guide*\n\n"
        "🍊 *TrumpWatch*\n"
        "/trumpwatch — Trigger immediate live poll\n"
        "/tw_recent — Last 10 alerts\n"
        "/tw_diag — Source health + dedup stats\n"
        "/tw_clear — Clear dedup cache (re-enables old posts)\n\n"
        "🏦 *FedWatch*\n"
        "/fedwatch — Next Fed event\n"
        "/fed_diag — Calendar + rate probability\n\n"
        "📊 *CryptoWatch*\n"
        "/cw_daily — Daily market brief\n"
        "/cw_weekly — Weekly sentiment\n\n"


        "🩺 *System*\n"
        "/health — Full system status\n"
        "/restart — Trigger clean poll of all modules\n"
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

# ── /cw_daily / /cw_weekly ───────────────────────────────────────────────
if text.startswith("/cw_daily"):
    if hasattr(cryptowatch_daily, "main"):
        cryptowatch_daily.main()
    else:
        send_text("📊 [CryptoWatch] Daily disabled (main() not found).")
    return

if text.startswith("/cw_weekly"):
    if hasattr(cryptowatch, "main"):
        cryptowatch.main()
    else:
        send_text("📊 [CryptoWatch] Weekly disabled (main() not found).")
    return
```

# ─── Entrypoint ──────────────────────────────────────────────────────────────

if **name** == “**main**”:
start_scheduler()
threading.Thread(target=command_loop, daemon=True).start()
print(“💬 Command loop started ✅”, flush=True)

```
# Keep process alive
while True:
    time.sleep(3600)
```