from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from bot.utils import send_text

# ---------------- CONFIG ----------------

TRUMPWATCH_STATE_PATH = os.environ.get(
    "TRUMPWATCH_STATE_PATH",
    "/var/data/trumpwatch_state.json",
)

TRUMPWATCH_TIMESPAN = os.environ.get("TRUMPWATCH_TIMESPAN", "3h")

KEYWORDS = [
    "venezuela", "sanction", "tariff", "china", "iran", "israel", "gaza",
    "ukraine", "russia", "nato", "oil", "opec", "fed", "powell",
    "rate", "inflation", "cpi", "jobs", "default", "debt", "shutdown",
    "treasury", "dollar", "bitcoin", "crypto", "sec",
]

HOT_WORDS = [
    "attack", "strike", "war", "ban", "emergency", "martial",
    "collapse", "sanctions", "tariffs", "charges", "indict",
    "bomb", "missile",
]

MAX_RECENT = 10
MAX_SEND_PER_RUN = 3

# ---------------- STATE ----------------

def _load_state() -> Dict[str, Any]:
    try:
        with open(TR