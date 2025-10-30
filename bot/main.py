import os, threading, time
from apscheduler.schedulers.background import BackgroundScheduler
from bot.utils import send_text, get_updates
from bot.modules import swingwatch, trumpwatch_live, fedwatch
def boot_banner(): send_text('✅ MacroWatch online — 🎯 SwingWatch (Bitget+Binance) | 🍊 High-Impact TrumpWatch | 🏦 FedWatch')
def schedule_jobs():
    sched = BackgroundScheduler(timezone='UTC')

    # 🎯 SwingWatch job every 4 hours
    if os.getenv('ENABLE_SWINGWATCH', 'true').lower() in ('1','true','yes','on'):
        sched.add_job(swingwatch.run_scan_post, 'cron', hour='0,4,8,12,16,20')

    # 🍊 TrumpWatch LIVE (dual-source)
    if os.getenv("ENABLE_TRUMPWATCH_LIVE","true").lower() in ("1","true","yes","on"):
        threading.Thread(target=trumpwatch_live.run_loop, daemon=True).start()

    # 🏦 FedWatch alerts
    if os.getenv('ENABLE_FEDWATCH','true').lower() in ('1','true','yes','on'):
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()

    sched.start()
def command_loop():
    offset=None
    while True:
        data=get_updates(offset=offset,timeout=20)
        for upd in data.get('result',[]):
            offset=upd['update_id']+1; msg=upd.get('message') or {}; text=(msg.get('text') or '').strip().lower(); chat=str(msg.get('chat',{}).get('id'))
            if not text or chat!=str(os.getenv('CHAT_ID')): continue
            if text.startswith('/next'): swingwatch.run_scan_post()
            elif text.startswith('/trumpwatch'): trumpwatch.post_mock(force=('force' in text))
            elif text.startswith('/tw_recent'): trumpwatch.show_recent()
            elif text.startswith('/fedwatch'): fedwatch.show_next_event()
        time.sleep(1)
if __name__=='__main__':
    print('🚀 MacroWatch starting...',flush=True); boot_banner(); schedule_jobs(); threading.Thread(target=command_loop,daemon=True).start()
    while True: time.sleep(3600)
