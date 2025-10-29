from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from bot import liquidity, trendline, confluence, alerts, state
import time, os

def run_zone_detection():
    zones = liquidity.get_weekly_liquidity_zones()
    tlines = trendline.get_strong_trendlines()
    confluence_zones = confluence.find_confluences(zones, tlines)

    # Auto-post setups if enabled
    if os.getenv("MOCK_AUTO_POST","true").lower() in ("1","true","yes","on"):
        for cz in confluence_zones:
            alerts.send_confluence_setup(cz)

    # Cache best per symbol for /next
    by_symbol = {}
    for cz in confluence_zones:
        s = cz.get("symbol")
        if s:
            by_symbol.setdefault(s, []).append(cz)
    for s, lst in by_symbol.items():
        best = confluence.pick_best_zone(lst)
        if best:
            state.set_snapshot(s, best)

    # Scan summary
    if os.getenv("POST_SCAN_SUMMARY","true").lower() in ("1","true","yes","on"):
        now_utc = datetime.utcnow().strftime("%H:%M UTC")
        count = len(confluence_zones)
        plural = "" if count == 1 else "s"
        msg = f"✅ SwingWatch Scan Complete\n🕒 {now_utc} | 4H Candle Close\n"
        msg += f"Detected {count} confluence{plural}."
        alerts.send_message(msg)

def run_price_monitor():
    confluence.check_price_hits(alerts.send_hit_alert, alerts.send_retest_alert)

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    # Every 4h at 00,04,08,12,16,20 UTC
    sched.add_job(run_zone_detection, 'cron', hour='0,4,8,12,16,20')
    sched.start()
    interval = int(float(os.getenv('PRICE_MONITOR_INTERVAL_SEC', '20')))
    while True:
        run_price_monitor()
        time.sleep(interval)
