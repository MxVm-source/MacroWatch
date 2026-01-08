"""
CryptoWatch Daily — short daily brief.

This module should NOT run schedulers or Telegram command loops.
`bot/main.py` is responsible for scheduling and commands.
"""

from datetime import datetime, timezone
from bot.utils import send_text


def main():
    """
    Called by APScheduler in bot/main.py (cron).
    Keep it short + actionable. No spam.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_text(
        "🧠📊 [CryptoWatch Daily]\n"
        f"🕒 {now}\n\n"
        "• BTC/ETH: check 4H structure + key levels\n"
        "• Liquidity: watch sweeps near prior highs/lows\n"
        "• Macro: Fed calendar + Trump headlines can flip bias\n\n"
        "Tip: Use /levels and /plan for the current map."
    )