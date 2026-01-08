from apscheduler.schedulers.background import BackgroundScheduler

from bot.modules import trumpwatch

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")

    # TrumpWatch auto-poll (every 2 minutes)
    scheduler.add_job(
        trumpwatch.post_mock,
        "interval",
        minutes=2,
        kwargs={"force": False},
        id="trumpwatch_poll",
        replace_existing=True,
    )

    scheduler.start()
    print("🕒 MacroWatch scheduler started")