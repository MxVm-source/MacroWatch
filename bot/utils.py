import os
import time

import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

# Telegram hard limit: 4096 chars per message
_MAX_LEN = 4096

# Max retries on 429 rate-limit responses
_MAX_RETRIES = 3


def send_text(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("send_text (dry-run):", text)
        return

    # Truncate gracefully if over Telegram's limit
    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN - 20] + "\n\n… [truncated]"

    payload = {
        "chat_id":                CHAT_ID,
        "text":                   text,
        "parse_mode":             "Markdown",   # all modules use *bold*, `code`, _italic_
        "disable_web_page_preview": True,        # suppress link previews (cleaner alerts)
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{API_BASE}/sendMessage",
                json=payload,
                timeout=10,
            )

            if resp.ok:
                return

            # 429 — Telegram rate limit: honour retry_after
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                print(f"send_text: rate limited, retrying in {retry_after}s (attempt {attempt}/{_MAX_RETRIES})")
                time.sleep(retry_after + 1)
                continue

            # Any other error — log and give up
            print(f"send_text error {resp.status_code}: {resp.text[:200]}")
            return

        except Exception as e:
            print(f"send_text exception (attempt {attempt}): {e}")
            if attempt < _MAX_RETRIES:
                time.sleep(2)

    print("send_text: gave up after max retries")


def get_updates(offset=None, timeout=20):
    if not TELEGRAM_TOKEN:
        return {"result": []}

    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset

    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params=params,
            timeout=timeout + 5,
        )
        if resp.ok:
            return resp.json()
        print(f"get_updates error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"get_updates exception: {e}")

    return {"result": []}
