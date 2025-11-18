# bot/main.py
import os
import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler

from bot.utils import send_text, get_updates
from bot.modules import swingwatch, trumpwatch, fedwatch
# NOTE: we import trumpwatch_live only inside __main__ so missing file won't break imports


def boot_banner():
    send_text("✅ MacroWatch online — 🎯 SwingWatch (Bitget+Binance) | 🍊 TrumpWatch | 🏦 FedWatch")


def start_scheduler():
    """Start cron/interval jobs (4H SwingWatch, 15m Trump mock, FedWatch loop)."""
    sched = BackgroundScheduler(timezone="UTC")

    # 🎯 SwingWatch every 4 hours
    if os.getenv("ENABLE_SWINGWATCH", "true").lower() in ("1", "true", "yes", "on"):
        sched.add_job(swingwatch.run_scan_post, "cron", hour="0,4,8,12,16,20")

    # 🍊 TrumpWatch mock interval (OPTIONAL; keep false when using LIVE)
    if os.getenv("ENABLE_TRUMPWATCH", "false").lower() in ("1", "true", "yes", "on"):
        minutes = int(os.getenv("TW_INTERVAL_MIN", "15"))
        sched.add_job(trumpwatch.post_mock, "interval", minutes=minutes)

    # 🏦 FedWatch alerts
    if os.getenv("ENABLE_FEDWATCH", "true").lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()

    sched.start()
    return sched


def command_loop():
    """Telegram commands: /next /trumpwatch [/force] /tw_recent /fedwatch"""
    offset = None
    while True:
        data = get_updates(offset=offset, timeout=20)
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip().lower()
            chat = str(msg.get("chat", {}).get("id"))
            if not text or chat != str(os.getenv("CHAT_ID")):
                continue

            if text.startswith("/next"):
                swingwatch.run_scan_post()

            elif text.startswith("/trumpwatch"):
                force = "force" in text
                trumpwatch.post_mock(force=force)

            elif text.startswith("/tw_recent"):
                trumpwatch.show_recent()

            elif text.startswith("/fedwatch"):
                fedwatch.show_next_event()

            elif text.startswith("/fed_diag"):
                fedwatch.show_diag()


        time.sleep(1)


if __name__ == "__main__":
    print("🚀 MacroWatch starting...", flush=True)
    boot_banner()
    start_scheduler()

    # 🍊 Start TrumpWatch Live (dual-source) — safe block
    # Runs in the SAME service without touching scheduler indentation
    try:
        if os.getenv("ENABLE_TRUMPWATCH_LIVE", "true").lower() in ("1", "true", "yes", "on"):
            from bot.modules import trumpwatch_live
            threading.Thread(target=trumpwatch_live.run_loop, daemon=True).start()
            print("🍊 TrumpWatch Live started ✅", flush=True)
        else:
            print("🍊 TrumpWatch Live disabled", flush=True)
    except Exception as e:
        print("⚠️ Error starting TrumpWatch Live:", e, flush=True)

    # Commands listener
    threading.Thread(target=command_loop, daemon=True).start()

    # Keep the process alive
    while True:
        time.sleep(3600)
