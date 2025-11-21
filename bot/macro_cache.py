# bot/macro_cache.py

import time
import logging

log = logging.getLogger("macro_cache")

# Simple in-memory macro snapshot.
# B+1 mode: no external APIs yet, just scaffolding.
MACRO_STATE = {
    "last_update": 0.0,
    "ttl_sec": 1800,  # 30 minutes
    "total_mc": None,
    "total_mc_24h": None,
    "dxy": None,
    "dxy_24h": None,
    "spx": None,
    "spx_24h": None,
}


def refresh_if_needed(force: bool = False) -> None:
    """
    Refresh macro snapshot at most once every ttl_sec.
    In B+1 mode, we do NOT call any external APIs yet.
    This function just updates the timestamp and keeps the
    structure ready for future data.
    """
    now = time.time()
    if not force and now - MACRO_STATE["last_update"] < MACRO_STATE["ttl_sec"]:
        return  # still fresh enough

    # In B+1, no external sources are configured.
    # If you later add real sources, plug them here.
    log.info("MacroCache: refresh called (B+1 mode, no external macro APIs configured).")

    MACRO_STATE["last_update"] = now
    # Other fields remain as-is (likely None).


def get_snapshot(force: bool = False) -> dict:
    """
    Return a shallow copy of the macro snapshot.
    If (force=True) or TTL expired, triggers a refresh.
    """
    refresh_if_needed(force=force)
    return MACRO_STATE.copy()
