import os
import time
from apscheduler.schedulers.background import BackgroundScheduler

from bot.modules import (
    fedwatch,
    trumpwatch,
    cryptowatch,
    cryptowatch_daily
)

scheduler = BackgroundScheduler(timezone="Europe/Brussels")


def start_schedulers():
    # ---------------------------
    # FEDWATCH (already existed)
    # ---------------------------
    scheduler.add_job(
        fedwatch.main,
        trigger="interval",
        minutes=1,
        id="fedwatch_task",
        max_instances=1,
        replace_existing=True
    )

    # ---------------------------
    # TRUMPWATCH (already existed)
    # ---------------------------
    scheduler.add_job(
        trumpwatch.main,
        trigger="interval",
        minutes=1,
        id="trumpwatch_task",
        max_instances=1,
        replace_existing=True
    )

    # ---------------------------
    # 🔵 CRYPTOWATCH DAILY (new)
    # Runs once per day at 15:28 Brussels (before US open)
    # ---------------------------
    scheduler.add_job(
        cryptowatch_daily.main,
        trigger="cron",
        hour=15,
        minute=28,
        id="cryptowatch_daily_task",
        max_instances=1,
        replace_existing=True
    )

    # ---------------------------
    # 🔵 CRYPTOWATCH WEEKLY (new)
    # Runs every Sunday at 18:00 Brussels
    # ---------------------------
    scheduler.add_job(
        cryptowatch.main,
        trigger="cron",
        day_of_week="sun",
        hour=18,
        minute=0,
        id="cryptowatch_weekly_task",
        max_instances=1,
        replace_existing=True
    )

    scheduler.start()


if __name__ == "__main__":
    print("🔥 MacroWatch Background Worker Starting...")
    start_schedulers()

    # Keep alive loop
    while True:
        time.sleep(60)

# trumpwatch_live imported in __main__


def boot_banner():
    send_text("✅ MacroWatch online — 🧠 CryptoWatch | 🍊 TrumpWatch | 🏦 FedWatch")



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
