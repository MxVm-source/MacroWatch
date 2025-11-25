import os
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from bot.utils import send_text, get_updates
from bot.modules import trumpwatch, fedwatch, cryptowatch, cryptowatch_daily
from bot.datafeed_bitget import get_position_report_safe

# trumpwatch_live imported in __main__

def boot_banner():
    send_text(
    "✅ MacroWatch rebooted\n"
    "All systems live: 🏦 FedWatch | 🍊 TrumpWatch | 📊 CryptoWatch\n"
    "If the market nukes, don’t blame us — blame your leverage."
)

def start_scheduler():
    """Start jobs for TrumpWatch mock, FedWatch loop, and CryptoWatch."""
    # Use Brussels timezone for scheduled jobs (cron). Interval jobs are unaffected by tz.
    sched = BackgroundScheduler(timezone="Europe/Brussels")

    # 🍊 TrumpWatch mock interval (OPTIONAL; keep false when using LIVE)
    if os.getenv("ENABLE_TRUMPWATCH", "false").lower() in ("1", "true", "yes", "on"):
        minutes = int(os.getenv("TW_INTERVAL_MIN", "15"))
        sched.add_job(trumpwatch.post_mock, "interval", minutes=minutes)

    # 🏦 FedWatch alerts (ICS + BTC/ETH reaction)
    if os.getenv("ENABLE_FEDWATCH", "true").lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()

    # 📉 CryptoWatch Daily – mini brief before U.S. market open
    # Default: enabled, 15:28 Brussels
    if os.getenv("ENABLE_CRYPTOWATCH_DAILY", "true").lower() in ("1", "true", "yes", "on"):
        sched.add_job(
            cryptowatch_daily.main,
            "cron",
            hour=15,
            minute=28,
            id="cryptowatch_daily_task",
            max_instances=1,
            replace_existing=True,
        )

    # 📊 CryptoWatch Weekly – full weekly sentiment report
    # Default: enabled, Sunday @ 18:00 Brussels
    if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() in ("1", "true", "yes", "on"):
        sched.add_job(
            cryptowatch.main,
            "cron",
            day_of_week="sun",
            hour=18,
            minute=0,
            id="cryptowatch_weekly_task",
            max_instances=1,
            replace_existing=True,
        )

     # 📘 Bitget Trade Watcher – detects your manual trades on Bitget
    if os.getenv("BITGET_ENABLED", "0") == "1":
        from bot.modules.datafeed_bitget import start_bitget_watcher
        threading.Thread(
            target=start_bitget_watcher,
            args=(send_text,),   # your existing Telegram sender 
            daemon=True
        ).start()

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

            # Optional: manual triggers for CryptoWatch
            elif text.startswith("/cw_daily"):
                cryptowatch_daily.main()

            elif text.startswith("/cw_weekly"):
                cryptowatch.main()
                
            elif text.startswith("/position"):
                 msg = get_position_report_safe()
                 send_text(msg)
            elif text.startswith("/tradewatch_status"):
                try:
                    from bot.modules.tradewatch import get_status
                    send_text(get_status())
                except Exception as e:
                    send_text(f"📈 [TradeWatch] Status unavailable: {e}")


if __name__ == "__main__":
    boot_banner()

    # Scheduler for TrumpWatch mock, FedWatch, CryptoWatch
    start_scheduler()

    # Start optional TrumpWatch Live in a dedicated thread
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
