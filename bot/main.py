import os
import threading
import time
from apscheduler.schedulers.background import BackgroundScheduler

from bot.utils import send_text, get_updates
from bot.modules import trumpwatch, fedwatch
# trumpwatch_live imported in __main__


def boot_banner():
    send_text("✅ MacroWatch online — 🍊 TrumpWatch | 🏦 FedWatch")


def start_scheduler():
    """Start jobs for optional TrumpWatch mock + FedWatch loop."""
    sched = BackgroundScheduler(timezone="UTC")

    # 🍊 TrumpWatch mock interval (OPTIONAL; keep false when using LIVE)
    if os.getenv("ENABLE_TRUMPWATCH", "false").lower() in ("1", "true", "yes", "on"):
        minutes = int(os.getenv("TW_INTERVAL_MIN", "15"))
        sched.add_job(trumpwatch.post_mock, "interval", minutes=minutes)

    # 🏦 FedWatch alerts (ICS + BTC/ETH reaction)
    if os.getenv("ENABLE_FEDWATCH", "true").lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()

    sched.start()
    return sched


def command_loop():
    """Telegram commands for MacroWatch."""
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

            if text.startswith("/trumpwatch"):
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

    # 🍊 Start TrumpWatch Live (dual-source, market-only filter)
    try:
        if os.getenv("ENABLE_TRUMPWATCH_LIVE", "true").lower() in ("1", "true", "yes", "on"):
            from bot.modules import trumpwatch_live
            threading.Thread(target=trumpwatch_live.run_loop, daemon=True).start()
            print("🍊 TrumpWatch Live started ✅", flush=True)
        else:
            print("🍊 TrumpWatch Live disabled", flush=True)
    except Exception as e:
        print("⚠️ Error starting TrumpWatch Live:", e, flush=True)

    # Telegram command listener
    threading.Thread(target=command_loop, daemon=True).start()

    # Keep service alive
    while True:
        time.sleep(3600)
