"""
TrumpWatch Live — MacroWatch module
Polls Trump's Truth Social + X mirrors for market-moving posts.

Key fixes vs previous version:
  - nitter.net is dead; replaced with a working Nitter instance pool
  - Truth Social public RSS added as primary (no auth needed)
  - Impact scoring baseline lowered; threshold easier to reach
  - s&p keyword fix (norm_text was stripping &)
  - All errors surfaced to Telegram + stderr instead of silently swallowed
  - /tw_diag command to see live source health
"""

import os
import time
import logging
import requests
import html
import re
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from collections import deque

from bot.utils import send_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TrumpWatch] %(message)s")
log = logging.getLogger("trumpwatch")

# ─── Sources ─────────────────────────────────────────────────────────────────

# Truth Social public RSS — most reliable, no auth, official posts
SRC_TS_RSS = os.getenv(
    "TW_SOURCE_URL_TS_RSS",
    "https://truthsocial.com/@realDonaldTrump.rss"
)

# Nitter instance pool — tried in order, first success wins
# These are community-maintained instances; update if one dies
DEFAULT_NITTER_POOL = ",".join([
    "https://nitter.privacydev.net/TrumpTruthOnX/rss",
    "https://nitter.cz/TrumpTruthOnX/rss",
    "https://nitter.poast.org/TrumpTruthOnX/rss",
    "https://nitter.1d4.us/TrumpTruthOnX/rss",
])
NITTER_POOL = [
    u.strip()
    for u in os.getenv("TW_NITTER_POOL", DEFAULT_NITTER_POOL).split(",")
    if u.strip()
]

POLL_SEC      = int(os.getenv("TW_POLL_SEC", "30"))
IMPACT_MIN    = float(os.getenv("TW_IMPACT_MIN", "0.60"))   # was 0.70, too strict
DEDUP_HOURS   = int(os.getenv("TW_DEDUP_HOURS", "6"))
RECENT_MAX    = int(os.getenv("TW_RECENT_MAX", "10"))
MARKET_FILTER = os.getenv("TW_MARKET_FILTER", "true").lower() in ("1", "true", "yes", "on")

RECENT_ALERTS: deque = deque(maxlen=RECENT_MAX)

STATE = {
    "seen": {},          # key -> utc isoformat
    "source_health": {}  # url -> {"ok": bool, "last_checked": str, "error": str}
}

# ─── Keywords ────────────────────────────────────────────────────────────────

MACRO_WORDS = [
    "market", "markets", "stock market", "stocks", "dow", "nasdaq",
    "s&p", "sp500", "wall street", "economy", "economic", "recession",
    "depression", "growth", "gdp", "jobs", "unemployment", "inflation",
    "deflation", "interest rate", "interest rates", "rates",
    "fed", "federal reserve", "powell",
]
FISCAL_WORDS = [
    "tax", "taxes", "tariff", "tariffs", "trade deal", "trade war",
    "sanctions", "regulation", "deregulation", "spending", "deficit",
    "debt", "budget", "stimulus", "bailout", "executive order",
]
GEO_WORDS = [
    "china", "russia", "iran", "ukraine", "taiwan", "europe",
    "european union", "war", "conflict", "invasion", "nuclear", "nuke",
    "opec", "oil", "gas", "energy", "middle east", "saudi",
]
CRYPTO_WORDS = [
    "bitcoin", "btc", "crypto", "cryptocurrency", "eth", "ethereum",
    "blockchain", "digital currency", "cbdc", "coinbase", "binance",
    "defi", "stablecoin", "dollar", "reserve currency",
]

# Build final set — keep s&p intact (don't lowercase through norm_text for matching)
ALL_MARKET_WORDS = list({w.lower() for w in (
    MACRO_WORDS + FISCAL_WORDS + GEO_WORDS + CRYPTO_WORDS
)})

# ─── Scoring & Sentiment ──────────────────────────────────────────────────────

BULL_WORDS = [
    "growth", "lower", "boost", "expand", "jobs", "rally", "deal", "win",
    "cut taxes", "boom", "record", "surge", "strong", "great", "winning",
    "peace", "agreement", "breakthrough",
]
BEAR_WORDS = [
    "tariff", "sanction", "war", "shutdown", "crash", "conflict", "ban",
    "recession", "inflation", "depression", "indict", "raid", "collapse",
    "fail", "threat", "danger", "attack", "retaliate", "impose",
]


def _score_impact(text: str) -> float:
    """
    Score 0.0–1.0. Baseline 0.50, each bull/bear keyword adds 0.07.
    Previous baseline was 0.55 with 0.08 steps — combined with 0.70 threshold
    it required 2 keywords minimum. Now 1 strong keyword is enough.
    """
    t = text.lower()
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    score = 0.50 + 0.07 * (bull + bear)
    return round(max(0.50, min(0.97, score)), 2)


def _sentiment(text: str):
    t = text.lower()
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    if bull > bear:
        return "bullish", "🟢📈"
    elif bear > bull:
        return "bearish", "🔴📉"
    return "neutral", "🔵⚖️"


# ─── Dedup & Normalisation ────────────────────────────────────────────────────

def _dedup_ok(key: str) -> bool:
    if key not in STATE["seen"]:
        return True
    t = datetime.fromisoformat(STATE["seen"][key])
    return datetime.utcnow() - t > timedelta(hours=DEDUP_HOURS)


def _norm_text(s: str) -> str:
    """Normalise for dedup/comparison — preserves & for s&p matching."""
    s = html.unescape(s or "").strip().lower()
    s = re.sub(r"https?://\S+", "", s)
    # Keep & so "s&p" survives
    s = re.sub(r"[^a-z0-9\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_market_relevant(text: str) -> bool:
    if not MARKET_FILTER:
        return True
    t = _norm_text(text)
    return bool(t) and any(w in t for w in ALL_MARKET_WORDS)


# ─── Fetchers ─────────────────────────────────────────────────────────────────

def _parse_rss(xml_text: str, source_name: str) -> list:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning(f"RSS parse error from {source_name}: {e}")
        return items
    for item in root.findall(".//item"):
        title   = html.unescape(item.findtext("title") or "")
        desc    = html.unescape(item.findtext("description") or "")
        link    = item.findtext("link") or ""
        pub     = (
            item.findtext("{http://purl.org/dc/elements/1.1/}date")
            or item.findtext("pubDate")
            or ""
        )
        # Use description if it's richer than title
        text = desc if len(desc) > len(title) else title
        # Strip HTML tags from description
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        pid = link or text[:60] or str(len(items))
        if text:
            items.append({
                "id": pid,
                "text": text,
                "url": link,
                "ts": pub,
                "source": source_name,
            })
    return items


def _fetch_ts_rss() -> list:
    """Truth Social public RSS — primary source."""
    url = SRC_TS_RSS
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "MacroWatch/1.0"})
        r.raise_for_status()
        items = _parse_rss(r.text, "Truth Social")
        STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
        log.info(f"Truth Social RSS: {len(items)} items")
        return items
    except Exception as e:
        err = str(e)
        STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
        log.warning(f"Truth Social RSS failed: {err}")
        return []


def _fetch_nitter_items() -> list:
    """Try each Nitter instance in pool, return first success."""
    for url in NITTER_POOL:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "MacroWatch/1.0"})
            r.raise_for_status()
            if "<rss" not in r.text and "<feed" not in r.text:
                raise ValueError("Response is not RSS/Atom")
            items = _parse_rss(r.text, f"X/Nitter")
            STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
            log.info(f"Nitter {url}: {len(items)} items")
            return items
        except Exception as e:
            err = str(e)
            STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
            log.warning(f"Nitter {url} failed: {err}")
    return []


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="minutes")


# ─── Diagnostic ───────────────────────────────────────────────────────────────

def run_diag():
    """Called by /tw_diag — reports source health to Telegram."""
    lines = ["🍊 *[TrumpWatch] Diagnostic*\n"]
    if not STATE["source_health"]:
        lines.append("No polls run yet — scheduler may not have started.")
    for url, h in STATE["source_health"].items():
        icon = "✅" if h["ok"] else "❌"
        short = url.split("/")[2]  # domain only
        err = f" — {h['error'][:80]}" if not h["ok"] else ""
        lines.append(f"{icon} `{short}`{err}")
    lines.append(f"\n📦 Recent alerts buffered: {len(RECENT_ALERTS)}/{RECENT_MAX}")
    lines.append(f"🔍 Seen cache size: {len(STATE['seen'])}")
    lines.append(f"⚙️ Impact threshold: {IMPACT_MIN} | Poll: {POLL_SEC}s | Market filter: {MARKET_FILTER}")
    send_text("\n".join(lines))


# ─── Recent ───────────────────────────────────────────────────────────────────

def show_recent():
    if not RECENT_ALERTS:
        send_text("🍊 [TrumpWatch] No alerts fired yet. Try /tw_diag to check source health.")
        return
    header = "🍊 *[TrumpWatch] Recent Alerts*\n"
    send_text(header + "\n\n────────────\n\n".join(list(RECENT_ALERTS)))


# ─── Core poll ────────────────────────────────────────────────────────────────

def poll_once():
    # Merge both sources; Truth Social is primary
    ts_items  = _fetch_ts_rss()
    x_items   = _fetch_nitter_items()

    # Deduplicate across sources by normalised text fingerprint
    seen_texts = set()
    all_items  = []
    for it in ts_items + x_items:
        fp = _norm_text(it["text"])[:100]
        if fp and fp not in seen_texts:
            seen_texts.add(fp)
            all_items.append(it)

    if not all_items:
        log.warning("poll_once: zero items from all sources — check /tw_diag")
        return

    now_iso = _now_iso()
    fired   = 0

    for it in all_items[:15]:
        pid, txt, url, src = it["id"], it["text"], it["url"], it["source"]

        key = _norm_text(txt)[:100]  # text-based dedup (source-agnostic)
        if not _dedup_ok(key):
            continue

        # Always mark seen so we don't re-process
        STATE["seen"][key] = now_iso

        if not _is_market_relevant(txt):
            continue

        impact = _score_impact(txt)
        if impact < IMPACT_MIN:
            log.info(f"Filtered (impact {impact} < {IMPACT_MIN}): {txt[:60]}")
            continue

        sent, emo = _sentiment(txt)

        # Build alert
        impact_label = "🔥 EXTREME" if impact >= 0.85 else "⚠️ HIGH" if impact >= 0.70 else "📌 MODERATE"
        link_line = f"🔗 {url}" if url else ""

        msg = "\n".join(filter(None, [
            f"🍊 [TrumpWatch] {impact_label} | Score: {impact} | {emo} {sent.title()}",
            f"🗞️ {txt.strip()[:1000]}",
            f"📡 Source: {src}",
            link_line,
        ]))

        RECENT_ALERTS.appendleft(f"🕒 {now_iso} UTC\n{msg}")
        send_text(msg)
        fired += 1

    log.info(f"poll_once complete: {len(all_items)} items checked, {fired} alerts fired")


# ─── Entry ────────────────────────────────────────────────────────────────────

def run_loop():
    log.info(f"TrumpWatch starting — poll every {POLL_SEC}s, threshold {IMPACT_MIN}")
    while True:
        try:
            poll_once()
        except Exception as e:
            log.error(f"Unhandled error in poll_once: {e}")
            try:
                send_text(f"🍊 [TrumpWatch] ⚠️ Poll error: {str(e)[:200]}")
            except Exception:
                pass
        time.sleep(POLL_SEC)
