import os, time, threading
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def log(msg):
    print(msg, flush=True)

def tg_send(text):
    import requests
    if not TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text, "parse_mode":"HTML"},
                      timeout=10)
    except Exception:
        pass

def heartbeat():
    if os.getenv("POST_HEARTBEAT","true").lower() in ("1","true","yes","on"):
        tz = os.getenv("TZ_LABEL","UTC")
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        tg_send(f"✅ FedWatch online — {now}")

def run_loop():
    interval = int(float(os.getenv("HEARTBEAT_MINUTES","60"))) * 60
    while True:
        log("tick")
        time.sleep(interval)
        heartbeat()

if __name__ == "__main__":
    log("🚀 FedWatch worker started")
    threading.Thread(target=run_loop, daemon=True).start()
    # keep alive
    while True:
        time.sleep(3600)
