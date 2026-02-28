"""
MacroWatch Scheduler
Manages all polling jobs via APScheduler.

Jobs:
  - TrumpWatch   : every 30s  (market-moving Trump posts)
  - FedWatch     : every 5min (Fed events & countdowns)
  - TradeWatch   : every 1min (live trade alerts from Bitget)
  - CryptoWatch  : every 5min (macro crypto signals)
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

log = logging.getLogger("scheduler")

# ─── Safe job wrappers ────────────────────────────────────────────────────────
# Each wrapper catches its own errors so one failing job never kills the others.

def _run_trumpwatch():
    try:
        from bot.modules import trumpwatch_live
        trumpwatch_live.poll_once()
    except Exception as e:
        log.error(f"[TrumpWatch] Job error: {e}")
        _notify(f"🍊 [TrumpWatch] Scheduler error: {str(e)[:200]}")


def _run_fedwatch():
    try:
        from bot.modules import fedwatch
        fedwatch.poll_once()
    except Exception as e:
        log.error(f"[FedWatch] Job error: {e}")


def _run_tradewatch():
    try:
        from bot.modules import tradewatch
        tradewatch.poll_once()
    except Exception as e:
        log.error(f"[TradeWatch] Job error: {e}")


def _run_cryptowatch():
    try:
        from bot.modules import cryptowatch
        cryptowatch.poll_once()
    except Exception as e:
        log.error(f"[CryptoWatch] Job error: {e}")


def _notify(msg: str):
    """Best-effort Telegram notification for scheduler-level errors."""
    try:
        from bot.utils import send_text
        send_text(msg)
    except Exception:
        pass


# ─── Error listener ───────────────────────────────────────────────────────────

def _on_job_error(event):
    log.error(f"APScheduler job {event.job_id} raised: {event.exception}")


# ─── Startup ──────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # ── TrumpWatch — 30s polls (was 2min, too slow for breaking news)
    scheduler.add_job(
        _run_trumpwatch,
        "interval",
        seconds=30,
        id="trumpwatch_poll",
        replace_existing=True,
        max_instances=1,        # never overlap
        misfire_grace_time=10,
    )

    # ── FedWatch — every 5 minutes
    scheduler.add_job(
        _run_fedwatch,
        "interval",
        minutes=5,
        id="fedwatch_poll",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    # ── TradeWatch — every 60 seconds
    scheduler.add_job(
        _run_tradewatch,
        "interval",
        seconds=60,
        id="tradewatch_poll",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=15,
    )

    # ── CryptoWatch — every 5 minutes
    scheduler.add_job(
        _run_cryptowatch,
        "interval",
        minutes=5,
        id="cryptowatch_poll",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.start()
    log.info("✅ MacroWatch scheduler started — jobs: trumpwatch(30s) | fedwatch(5m) | tradewatch(60s) | cryptowatch(5m)")
    print("🕒 MacroWatch scheduler started")


# ─── Runtime controls (callable from Telegram commands if needed) ─────────────

def pause_job(job_id: str):
    from apscheduler.schedulers.background import BackgroundScheduler
    # Access via module-level scheduler if you promote it; placeholder for now
    pass


def get_job_status() -> str:
    """Returns a status string for all jobs — useful for a /scheduler_diag command."""
    lines = ["📅 *Scheduler Job Status*\n"]
    # Import here to avoid circular; scheduler instance would need to be module-level
    # to fully implement this — left as extension point
    lines.append("Use /tw_diag for TrumpWatch health.")
    lines.append("Use /fed_diag for FedWatch health.")
    return "\n".join(lines)
