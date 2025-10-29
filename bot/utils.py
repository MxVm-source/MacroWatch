import os, requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

def send_text(text: str):
    if not BASE or not CHAT_ID: return False
    try:
        r = requests.post(f"{BASE}/sendMessage", data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        return r.ok
    except Exception:
        return False

def send_photo(caption: str, filepath: str):
    if not BASE or not CHAT_ID: return False
    try:
        with open(filepath, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            r = requests.post(f"{BASE}/sendPhoto", data=data, files=files, timeout=30)
        return r.ok
    except Exception:
        return False

def get_updates(offset=None, timeout=20):
    if not BASE: return {"ok":False,"result":[]}
    try:
        params = {"timeout": timeout}
        if offset is not None: params["offset"] = offset
        r = requests.get(f"{BASE}/getUpdates", params=params, timeout=timeout+5)
        return r.json()
    except Exception:
        return {"ok":False,"result":[]}
