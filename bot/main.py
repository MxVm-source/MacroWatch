import time
from apscheduler.schedulers.background import BackgroundScheduler

from bot.utils import send_text, get_updates  # keep if you use them elsewhere

from bot.modules import (
    fedwatch,
    trumpwatch,
    cryptowatch,
    cryptowatch_daily,
)
# trumpwatch_live will be imported in __main__ if you want live mode


# Use Brussels timezone for all jobs
scheduler = BackgroundScheduler(timezone="Europe/Brussels")


def start_schedulers():
    # ---------------------------
    # 🏦 FedWatch – periodic checks
    # ---------------------------
    scheduler.add_job(
        fedwatch.main,
        trigger="interval",
        minutes=1,
        id="fedwatch_task",
        max_instances=1,
        replace_existing=True,
    )

    # ---------------------------
    # 🧨 TrumpWatch – periodic checks
    # ---------------------------
    scheduler.add_job(
        trumpwatch.main,
        trigger="interval",
        minutes=1,
        id="trumpwatch_task",
        max_instances=1,
        replace_existing=True,
    )

    # ---------------------------
    # 📉 CryptoWatch Daily – mini brief
    # Runs every day at 15:28 Brussels (before US cash open)
    # ---------------------------
    scheduler.add_job(
        cryptowatch_daily.main,
        trigger="cron",
        hour=15,
        minute=28,
        id="cryptowatch_daily_task",
        max_instances=1,
        replace_existing=True,
    )

    # ---------------------------
    # 📊 CryptoWatch Weekly – full sentiment report
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
        replace_existing=True,
    )

    scheduler.start()


if __name__ == "__main__":
    print("🔥 MacroWatch Background Worker starting...")
    start_schedulers()

    # Optional: start trumpwatch_live in parallel if you have a live mode
    # from bot.modules import trumpwatch_live
    # trumpwatch_live.start()  # or whatever entrypoint you use there

    # Simple keep-alive loop for the worker
    while True:
        time.sleep(60)
