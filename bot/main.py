# bot/main.py
"""
MacroWatch — Entry point

Polling architecture (all via APScheduler, no raw threads for polling):
  TrumpWatch   → every 60s
  FedWatch     → every 5min
  CryptoWatch  → weekly cron (Sunday 18:00)
  CryptoDaily  → daily cron  (15:28)
  TradeWatch   → own threads (event-driven, kept as-is)

Command loop runs in a single daemon thread.
All poll functions are wrapped so one crash never kills the scheduler.
"""

import os
import sys
import json
import platform
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR

from bot.utils import send_text, get_updates
from bot.datafeed_bitget import (
    get_position_report_safe,
    build_positions_and_orders_message,
    build_open_orders_message,
)

import bot.modules.fedwatch        as fedwatch
import bot.modules.cryptowatch     as cryptowatch
import bot.modules.cryptowatch_daily as cryptowatch_daily
import bot.modules.trumpwatch_live as trumpwatch_live

STARTED_AT_UTC = datetime.now(timezone.utc)

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

def _job_cryptowatch_daily():
    try:
        if hasattr(cryptowatch_daily, "main"):
            cryptowatch_daily.main()
    except Exception as e:
        _err("CryptoWatch Daily", e)

def _job_cryptowatch_weekly():
    try:
        if hasattr(cryptowatch, "main"):
            cryptowatch.main()
    except Exception as e:
        _err("CryptoWatch Weekly", e)

def _err(module: str, exc: Exception):
    msg = f"⚠️ [{module}] Job error: {str(exc)[:200]}"
    print(msg, flush=True)
    try:
        send_text(msg)
    except Exception:
        pass


# ─── Scheduler setup ─────────────────────────────────────────────────────────

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

    # ── CryptoWatch Daily — 15:28 every day
    if os.getenv("ENABLE_CRYPTOWATCH_DAILY", "true").lower() in ("1", "true", "yes", "on"):
        SCHED.add_job(
            _job_cryptowatch_daily, "cron", hour=15, minute=28,
            id="cryptowatch_daily", max_instances=1,
        )
        print("📊 CryptoWatch Daily scheduled (15:28) ✅", flush=True)

    # ── CryptoWatch Weekly — Sunday 18:00
    if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() in ("1", "true", "yes", "on"):
        SCHED.add_job(
            _job_cryptowatch_weekly, "cron", day_of_week="sun", hour=18, minute=0,
            id="cryptowatch_weekly", max_instances=1,
        )
        print("📊 CryptoWatch Weekly scheduled (Sun 18:00) ✅", flush=True)

    SCHED.start()
    print("🕒 APScheduler started ✅", flush=True)


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


# ─── Command loop ─────────────────────────────────────────────────────────────

def command_loop():
    offset     = None
    chat_allow = str(os.getenv("CHAT_ID") or "")

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


def _handle_command(text: str, text_raw: str):

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

            "📑 *Positions & Orders*\n"
            "/position — Current Bitget futures positions\n"
            "/orders [SYMBOL] — Open TP/SL orders\n"
            "/pos_orders — Positions + orders combined\n\n"
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

    # ── /position ─────────────────────────────────────────────────────────────
    if text.startswith("/position"):
        send_text(get_position_report_safe())
        return

    # ── /orders ───────────────────────────────────────────────────────────────
    if text.startswith("/orders"):
        parts = text_raw.split()
        sym   = parts[1].strip().upper() if len(parts) > 1 else None
        send_text(build_open_orders_message(sym) if sym else build_positions_and_orders_message())
        return

    # ── /pos_orders ───────────────────────────────────────────────────────────
    if text.startswith("/pos_orders"):
        send_text(build_positions_and_orders_message())
        return




# ─── Entrypoint ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
    threading.Thread(target=command_loop, daemon=True).start()
    print("💬 Command loop started ✅", flush=True)

    # Keep process alive
    while True:
        time.sleep(3600)
