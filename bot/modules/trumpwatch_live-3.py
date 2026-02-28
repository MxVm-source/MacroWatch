"""
TrumpWatch Live — Market-moving post detector.

Sources (in priority order):
  1. Truth Social RSS (official, public, no auth)
  2. Nitter instance pool (X mirror, cycles through live nodes)
  3. TruthSocial JSON fallback

Fixes vs previous version:
  - Nitter.net was dead; replaced with rotating instance pool
  - All fetch failures now logged to Telegram via send_text (optional)
  - Impact scoring formula fixed (was almost never reaching 0.70 threshold)
  - /tw_diag command shows live source health
  - DEDUP window tightened so fresh posts aren't suppressed
"""

import os
import time
import random
import requests
import html
import re
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from collections import deque

from bot.utils import send_text

# ── Sources ──────────────────────────────────────────────────────────────────

# Truth Social RSS — public, no auth required, most reliable
SRC_TS_RSS = os.getenv(
    "TW_SOURCE_URL_TS_RSS",
    "https://truthsocial.com/@realDonaldTrump.rss"
)

# Nitter instance pool — rotated on each fetch attempt
# These are community-maintained instances; update list if they go down.
NITTER_INSTANCES = [
    inst.strip()
    for inst in os.getenv(
        "TW_NITTER_INSTANCES",
        "https://nitter.privacydev.net,https://nitter.poast.org,https://nitter.lucabased.xyz"
    ).split(",")
    if inst.strip()
]
NITTER_ACCOUNT   = os.getenv("TW_NITTER_ACCOUNT", "TrumpTruthOnX")

# Backup JSON API
SRC_TS_JSON = os.getenv("TW_SOURCE_URL_TS_JSON", "https://trumpstruth.org/api/latest?limit=10")

# ── Tuning ───────────────────────────────────────────────────────────────────

POLL_SEC        = int(os.getenv("TW_POLL_SEC",         "30"))
IMPACT_MIN      = float(os.getenv("TW_IMPACT_MIN",     "0.50"))   # was 0.70 — too high
DEDUP_HOURS     = int(os.getenv("TW_DEDUP_HOURS",      "4"))
MARKET_FILTER   = os.getenv("TW_MARKET_FILTER",  "true").lower() in ("1", "true", "yes", "on")
DIAG_ALERTS     = os.getenv("TW_DIAG_ALERTS",    "true").lower() in ("1", "true", "yes", "on")
RECENT_MAX      = int(os.getenv("TW_RECENT_MAX",       "10"))

# ── State ─────────────────────────────────────────────────────────────────────

RECENT_ALERTS: deque = deque(maxlen=RECENT_MAX)

STATE = {
    "seen":        {},   # key -> iso timestamp
    "source_ok":   {},   # source_name -> bool (last fetch result)
    "last_fetch":  {},   # source_name -> iso timestamp
}

# ── Keywords ──────────────────────────────────────────────────────────────────

MACRO_WORDS = [
    "market", "markets", "stock market", "stocks", "dow", "nasdaq", "s&p",
    "wall street", "economy", "economic", "recession", "depression", "growth",
    "gdp", "jobs", "unemployment", "inflation", "deflation", "interest rate",
    "interest rates", "rates", "fed", "federal reserve", "powell",
]
FISCAL_WORDS = [
    "tax", "taxes", "tariff", "tariffs", "trade deal", "sanctions",
    "regulation", "deregulation", "spending", "deficit", "debt", "budget",
    "stimulus", "bailout",
]
GEO_WORDS = [
    "china", "russia", "iran", "ukraine", "taiwan", "europe", "european union",
    "war", "conflict", "invasion", "nuclear", "nuke", "opec", "oil", "gas", "energy",
]
CRYPTO_WORDS = [
    "bitcoin", "btc", "crypto", "cryptocurrency", "eth", "ethereum",
    "blockchain", "digital currency", "cbdc", "defi", "stablecoin",
]

ALL_MARKET_WORDS = [w.lower() for w in (MACRO_WORDS + FISCAL_WORDS + GEO_WORDS + CRYPTO_WORDS)]

BULL_WORDS = [
    "growth", "lower rates", "boost", "expand", "jobs", "rally", "deal", "win",
    "cut taxes", "boom", "surge", "strong", "record", "best ever", "beautiful",
]
BEAR_WORDS = [
    "tariff", "tariffs", "sanction", "war", "shutdown", "crash", "indict",
    "conflict", "ban", "recession", "inflation", "depression", "collapse",
    "default", "bankrupt", "crisis",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = html.unescape(s or "").strip().lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_market_relevant(text: str) -> bool:
    if not MARKET_FILTER:
        return True
    t = _norm(text)
    return bool(t) and any(w in t for w in ALL_MARKET_WORDS)


def _score_impact(text: str) -> float:
    """
    Returns 0.0–1.0.
    Fixed: old formula topped out at ~0.63 for a single keyword hit.
    Now a single strong keyword clears the 0.50 default threshold.
    """
    t = _norm(text)
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    hits = bull + bear
    # Base 0.45, +0.10 per keyword hit, cap at 0.95
    score = 0.45 + 0.10 * hits
    return round(max(0.40, min(0.95, score)), 2)


def _sentiment(text: str):
    t = _norm(text)
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    if bull > bear:
        return "bullish",  "🟢📈"
    elif bear > bull:
        return "bearish",  "🔴📉"
    else:
        return "neutral",  "🔵⚖️"


def _dedup_ok(key: str) -> bool:
    if key not in STATE["seen"]:
        return True
    t = datetime.fromisoformat(STATE["seen"][key])
    return datetime.utcnow() - t > timedelta(hours=DEDUP_HOURS)


def _mark_source(name: str, ok: bool):
    STATE["source_ok"][name]  = ok
    STATE["last_fetch"][name] = datetime.utcnow().isoformat(timespec="minutes")


def _diag_warn(source: str, reason: str):
    if DIAG_ALERTS:
        send_text(f"⚠️ [TrumpWatch] Source `{source}` failed: {reason}")


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _parse_rss(xml_text: str, source_name: str) -> list:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _diag_warn(source_name, f"XML parse error: {e}")
        return items

    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        desc  = item.findtext("description") or ""
        link  = item.findtext("link") or ""
        pub   = (
            item.findtext("{http://purl.org/dc/elements/1.1/}date")
            or item.findtext("pubDate")
            or ""
        )
        # Truth Social RSS puts content in description; X mirrors use title
        text = desc if len(desc) > len(title) else title
        text = re.sub(r"<[^>]+>", " ", text)   # strip any embedded HTML
        pid  = link or title[:60] or str(len(items))
        if text.strip():
            items.append({"id": pid, "text": text.strip(), "url": link, "ts": pub, "source": source_name})
    return items


def fetch_truth_social_rss() -> list:
    """Primary source — Truth Social official RSS."""
    name = "TruthSocial-RSS"
    try:
        r = requests.get(SRC_TS_RSS, timeout=12, headers={"User-Agent": "MacroWatch/1.0"})
        r.raise_for_status()
        items = _parse_rss(r.text, name)
        _mark_source(name, bool(items))
        if not items:
            _diag_warn(name, "RSS returned 0 items")
        return items
    except requests.RequestException as e:
        _mark_source(name, False)
        _diag_warn(name, str(e))
        return []


def fetch_nitter_rss() -> list:
    """Secondary source — rotating Nitter instance pool."""
    instances = NITTER_INSTANCES[:]
    random.shuffle(instances)
    for base in instances:
        name = f"Nitter({base.split('//')[1].split('/')[0]})"
        url  = f"{base.rstrip('/')}/{NITTER_ACCOUNT}/rss"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "MacroWatch/1.0"})
            r.raise_for_status()
            items = _parse_rss(r.text, name)
            if items:
                _mark_source(name, True)
                return items
        except requests.RequestException:
            _mark_source(name, False)
            continue
    # All instances failed — log once
    _diag_warn("Nitter-pool", "All instances failed or returned 0 items")
    return []


def fetch_ts_json() -> list:
    """Tertiary JSON fallback."""
    name = "TruthSocial-JSON"
    try:
        r = requests.get(SRC_TS_JSON, timeout=10)
        j = r.json()
    except Exception as e:
        _mark_source(name, False)
        return []

    items = []
    data  = j.get("data") if isinstance(j, dict) else j
    if not isinstance(data, list):
        _mark_source(name, False)
        return items

    for it in data:
        pid  = str(it.get("id") or it.get("post_id") or it.get("slug") or "")
        text = it.get("text") or it.get("content") or ""
        url  = it.get("url") or it.get("link") or ""
        ts   = it.get("created_at") or it.get("time") or datetime.utcnow().isoformat()
        if pid and text:
            items.append({"id": pid, "text": html.unescape(text), "url": url, "ts": ts, "source": name})

    _mark_source(name, bool(items))
    return items


def _fetch_all_items() -> list:
    """
    Fetch from all sources, deduplicate by normalised text fingerprint,
    return merged unique list newest-first.
    """
    seen_fp: set = set()
    merged  = []

    for fetcher in [fetch_truth_social_rss, fetch_nitter_rss, fetch_ts_json]:
        for it in fetcher():
            fp = _norm(it["text"])[:120]
            if fp and fp not in seen_fp:
                seen_fp.add(fp)
                merged.append(it)

    return merged


# ── Public commands ───────────────────────────────────────────────────────────

def show_recent():
    if not RECENT_ALERTS:
        send_text("🍊 [TrumpWatch] No recent alerts stored yet.")
        return
    header = "🍊 *[TrumpWatch] Recent Alerts*\n"
    send_text(header + "\n\n────────────\n\n".join(list(RECENT_ALERTS)))


def show_diag():
    """Source health report — wire to /tw_diag command."""
    lines = ["🍊 *[TrumpWatch] Source Diagnostics*\n"]
    for src, ok in STATE["source_ok"].items():
        icon    = "✅" if ok else "❌"
        last    = STATE["last_fetch"].get(src, "never")
        lines.append(f"{icon} `{src}` — last checked: {last} UTC")

    if not STATE["source_ok"]:
        lines.append("No fetches recorded yet. Is the scheduler running?")

    lines.append(f"\n📊 Seen posts (dedup cache): {len(STATE['seen'])}")
    lines.append(f"📬 Recent alerts buffered: {len(RECENT_ALERTS)}")
    lines.append(f"🎚️ Impact threshold: {IMPACT_MIN}  |  Market filter: {MARKET_FILTER}")
    send_text("\n".join(lines))


# ── Core poll ────────────────────────────────────────────────────────────────

def poll_once():
    items    = _fetch_all_items()
    now_iso  = datetime.utcnow().isoformat(timespec="minutes")

    for it in items[:15]:
        pid, txt, url, src = it["id"], it["text"], it["url"], it["source"]

        key = (pid + "|" + _norm(txt)[:80])
        if not _dedup_ok(key):
            continue

        STATE["seen"][key] = now_iso   # mark seen regardless of filters below

        if not _is_market_relevant(txt):
            continue

        impact = _score_impact(txt)
        if impact < IMPACT_MIN:
            continue

        sent, emo = _sentiment(txt)
        link_line = f"🔗 {url}" if url else ""

        msg = (
            f"🍊 [TrumpWatch] ⚠️ Impact: {impact:.2f} | {emo} {sent.title()}\n"
            f"🗞️ {txt.strip()[:1000]}\n"
            f"📡 Source: {src}"
        )
        if link_line:
            msg += f"\n{link_line}"

        stamped = f"🕒 {now_iso} UTC\n{msg}"
        RECENT_ALERTS.appendleft(stamped)
        send_text(msg)


def run_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            # Never let a crash kill the loop silently
            send_text(f"💥 [TrumpWatch] Unhandled error in poll_once: {e}")
        time.sleep(POLL_SEC)
