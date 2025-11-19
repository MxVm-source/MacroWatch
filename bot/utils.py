import os
import requests
import time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""


def send_text(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("send_text (dry-run):", text)
        return
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        if not resp.ok:
            print("send_text error:", resp.status_code, resp.text)
    except Exception as e:
        print("send_text exception:", e)


def get_updates(offset=None, timeout=20):
    if not TELEGRAM_TOKEN:
        return {"result": []}
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=timeout+5)
        if resp.ok:
            return resp.json()
        print("get_updates error:", resp.status_code, resp.text)
    except Exception as e:
        print("get_updates exception:", e)
    return {"result": []}
