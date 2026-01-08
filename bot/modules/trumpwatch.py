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
        with open(TRUMPWATCH_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("seen", [])
        data.setdefault("recent", [])
        return data
    except Exception:
        return {"seen": [], "recent": []}


def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(TRUMPWATCH_STATE_PATH), exist_ok=True)
    with open(TRUMPWATCH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _stable_id(source: str, title: str, url: str) -> str:
    raw = f"{source}|{title}|{url}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]

# ---------------- FILTERING ----------------

def _is_market_moving(title: str) -> bool:
    t = title.lower()
    kw_hits = [k for k in KEYWORDS if k in t]
    if not kw_hits:
        return False

    hot = any(w in t for w in HOT_WORDS)
    return hot or len(kw_hits) >= 2


def _format_alert(source: str, title: str, url: str) -> str:
    return (
        "🍊 [TrumpWatch] Market-moving headline\n"
        f"🗞️ {title}\n"
        f"🔗 {url}\n"
        f"🏷️ source: {source}"
    )

# ---------------- FETCH ----------------

def _fetch_gdelt_articles() -> List[Dict[str, str]]:
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    query = (
        '(Trump OR "Donald Trump" OR "Truth Social") '
        '(Venezuela OR sanctions OR tariffs OR China OR Iran OR Fed OR Powell '
        'OR inflation OR shutdown OR debt OR oil OR OPEC OR bitcoin OR crypto OR SEC)'
    )
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": 30,
        "timespan": TRUMPWATCH_TIMESPAN,
        "sort": "HybridRel",
    }

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    out: List[Dict[str, str]] = []
    for a in data.get("articles", []) or []:
        title = (a.get("title") or "").strip()
        link = (a.get("url") or "").strip()
        domain = (a.get("domain") or "news").strip()
        if title and link:
            out.append({"source": domain, "title": title, "url": link})
    return out

# ---------------- PUBLIC API ----------------

def post_mock(force: bool = False):
    """
    Backwards-compatible name.
    Now pulls REAL Trump headlines.
    """
    state = _load_state()
    seen = set(state.get("seen", []))
    recent = list(state.get("recent", []))

    try:
        articles = _fetch_gdelt_articles()
    except Exception as e:
        send_text("🍊 [TrumpWatch] Error fetching headlines.")
        return

    sent = 0
    for it in articles:
        item_id = _stable_id(it["source"], it["title"], it["url"])
        if item_id in seen:
            continue

        if not force and not _is_market_moving(it["title"]):
            continue

        seen.add(item_id)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        recent.append(f"{ts} | {it['title']} | {it['url']}")
        recent = recent[-MAX_RECENT:]

        send_text(_format_alert(it["source"], it["title"], it["url"]))
        sent += 1
        time.sleep(0.4)

        if sent >= MAX_SEND_PER_RUN:
            break

    state["seen"] = list(seen)[-200:]
    state["recent"] = recent
    _save_state(state)

    if sent == 0:
        send_text("🍊 [TrumpWatch] No market-moving Trump headlines detected.")


def show_recent():
    state = _load_state()
    recent = state.get("recent", [])
    if not recent:
        send_text("🍊 [TrumpWatch] No recent alerts stored.")
        return

    lines = ["🍊 [TrumpWatch] Recent alerts:"]
    for r in recent[-5:]:
        lines.append(f"• {r}")
    send_text("\n".join(lines))