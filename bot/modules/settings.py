# settings.py
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRYPTOWATCH_CHAT_ID = os.getenv("CRYPTOWATCH_CHAT_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Brussels")
CRYPTOWATCH_ENABLED = os.getenv("CRYPTOWATCH_ENABLED", "true").lower() == "true"
