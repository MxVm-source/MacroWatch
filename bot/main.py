import os, threading, time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from bot.utils import send_text, get_updates
from bot.modules import swingwatch, trumpwatch, fedwatch

def boot_banner():
    send_text("✅ MacroWatchBot online — monitoring 🎯 SwingWatch | 🍊 TrumpWatch | 🏦 FedWatch")

def schedule_jobs():
    sched = BackgroundScheduler(timezone="UTC")
    if os.getenv("ENABLE_SWINGWATCH","true").lower() in ("1","true","yes","on"):
        # 4H scans at 00,04,08,12,16,20 UTC
        sched.add_job(swingwatch.run_scan_post, 'cron', hour='0,4,8,12,16,20')
    if os.getenv("ENABLE_TRUMPWATCH","true").lower() in ("1","true","yes","on"):
        # every 15 min default
        minutes = int(os.getenv("TW_INTERVAL_MIN", "15"))
        sched.add_job(trumpwatch.post_mock, 'interval', minutes=minutes)
    if os.getenv("ENABLE_FEDWATCH","true").lower() in ("1","true","yes","on"):
        # fedwatch alert loop in separate thread
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()
    sched.start()

def command_loop():
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
                swingwatch.show_latest()
            elif text.startswith("/trumpwatch"):
                trumpwatch.post_mock(force=True)
            elif text.startswith("/tw_recent"):
                trumpwatch.show_recent()
            elif text.startswith("/fedwatch"):
                fedwatch.show_next_event()
        time.sleep(1)

if __name__ == "__main__":
    print("🚀 MacroWatchBot starting...", flush=True)
    boot_banner()
    schedule_jobs()
    threading.Thread(target=command_loop, daemon=True).start()
    # keep alive
    while True:
        time.sleep(3600)
