# bot/modules/srwatch.py
"""S&RWatch — DISABLED (stub version to stop alert spam)"""

import logging
from datetime import datetime, timezone

from bot.utils import send_text

log = logging.getLogger("srwatch")

STATE = {
    "last_check":     None,
    "daily_levels":   {},
    "weekly_levels":  {},
    "monthly_levels": {},
    "last_price":     None,
    "last_4h_close":  None,
    "approaching":    {},
    "hit":            {},
    "breakout":       {},
    "breakout_time":  {},
}


def poll_once():
    """Disabled — no alerts."""
    STATE["last_check"] = datetime.now(timezone.utc)
    return


def show_levels():
    send_text("📐 S&RWatch is currently disabled. Will be re-enabled after tuning.")


def show_diag():
    send_text("📐 S&RWatch is DISABLED — alert spam fix in progress.")


def refresh_levels():
    pass
