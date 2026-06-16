# ─────────────────────────────────────────────────────────────────────────────
# APPEND TO bot/utils.py
# Inline-keyboard + callback helpers for the stage/approve loop.
# Reuses the module globals already defined above: TELEGRAM_TOKEN, CHAT_ID,
# API_BASE, requests, _MAX_LEN.
# ─────────────────────────────────────────────────────────────────────────────

def send_buttons(text: str, buttons):
    """
    Send a message with an inline keyboard.
      buttons = [[(label, callback_data), ...], ...]   # rows of (label, data)
    Returns Telegram's JSON response (so the caller can read result.message_id).
    """
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("send_buttons (dry-run):", text, buttons)
        return {"result": {"message_id": 0}}

    if len(text) > _MAX_LEN:
        text = text[:_MAX_LEN - 20] + "\n\n… [truncated]"

    keyboard = [[{"text": lbl, "callback_data": data} for (lbl, data) in row]
                for row in buttons]
    payload = {
        "chat_id":      CHAT_ID,
        "text":         text,
        "parse_mode":   "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        if resp.ok:
            return resp.json()
        print(f"send_buttons error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"send_buttons exception: {e}")
    return {"result": {"message_id": 0}}


def edit_message_text(message_id, text: str):
    """Replace the text of an existing message (used to update a card after a tap)."""
    if not TELEGRAM_TOKEN or not CHAT_ID or not message_id:
        print("edit_message_text (dry-run):", message_id, text)
        return
    payload = {
        "chat_id":    CHAT_ID,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(f"{API_BASE}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        print(f"edit_message_text exception: {e}")


def answer_callback_query(callback_query_id, text: str = ""):
    """Acknowledge a button tap so Telegram stops the loading spinner."""
    if not TELEGRAM_TOKEN or not callback_query_id:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{API_BASE}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"answer_callback_query exception: {e}")
