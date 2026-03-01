"""
TrumpWatch Live — MacroWatch module
Polls Trump's Truth Social posts for market-moving content.

Sources (confirmed working as of 2026):
  1. trumpstruth.org/feed  — RSS, updated every few minutes, no auth ✅
  2. ix.cnn.io JSON        — CNN's archive, updated every 5 min, no auth ✅

Dead sources (do not use):
  - truthsocial.com RSS   → 403 Forbidden (blocks non-browser requests)
  - nitter.net            → shut down permanently
  - All Nitter instances  → returning 0 items
  - trumpstruth.org/api/latest → endpoint does not exist
"""

import os
import time
import logging
import requests
import html
import re
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from collections import deque

from bot.utils import send_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TrumpWatch] %(message)s")
log = logging.getLogger("trumpwatch")

# ─── Sources ─────────────────────────────────────────────────────────────────

# PRIMARY: trumpstruth.org RSS — public archive, updated every few minutes
SRC_RSS = os.getenv("TW_SOURCE_RSS", "https://www.trumpstruth.org/feed")

# SECONDARY: CNN Truth Social archive — updated every 5 minutes, pure JSON
SRC_CNN_JSON = os.getenv(
    "TW_SOURCE_CNN",
    "https://ix.cnn.io/data/truth-social/truth_archive.json"
)

# ─── Config ──────────────────────────────────────────────────────────────────

POLL_SEC      = int(os.getenv("TW_POLL_SEC", "60"))
IMPACT_MIN    = float(os.getenv("TW_IMPACT_MIN", "0.60"))
DEDUP_HOURS   = int(os.getenv("TW_DEDUP_HOURS", "6"))
RECENT_MAX    = int(os.getenv("TW_RECENT_MAX", "10"))
MARKET_FILTER = os.getenv("TW_MARKET_FILTER", "true").lower() in ("1", "true", "yes", "on")
CNN_RECENT_N  = int(os.getenv("TW_CNN_RECENT_N", "20"))

RECENT_ALERTS: deque = deque(maxlen=RECENT_MAX)

STATE = {
    "seen": {},
    "source_health": {},
}

# ─── Keywords ────────────────────────────────────────────────────────────────

MACRO_WORDS = [
    "market", "markets", "stock market", "stocks", "dow", "nasdaq",
    "s&p", "sp500", "wall street", "economy", "economic", "recession",
    "growth", "gdp", "jobs", "unemployment", "inflation", "deflation",
    "interest rate", "rates", "fed", "federal reserve", "powell",
]
FISCAL_WORDS = [
    "tax", "taxes", "tariff", "tariffs", "trade deal", "trade war",
    "sanctions", "regulation", "deregulation", "spending", "deficit",
    "debt", "budget", "stimulus", "bailout", "executive order",
]
GEO_WORDS = [
    "china", "russia", "iran", "ukraine", "taiwan", "europe",
    "war", "conflict", "invasion", "nuclear", "nuke",
    "opec", "oil", "gas", "energy", "middle east", "saudi",
]
CRYPTO_WORDS = [
    "bitcoin", "btc", "crypto", "cryptocurrency", "eth", "ethereum",
    "blockchain", "digital currency", "cbdc", "coinbase", "binance",
    "stablecoin", "dollar", "reserve currency",
]

ALL_MARKET_WORDS = list({w.lower() for w in (
    MACRO_WORDS + FISCAL_WORDS + GEO_WORDS + CRYPTO_WORDS
)})

BULL_WORDS = [
    "growth", "lower", "boost", "expand", "jobs", "rally", "deal", "win",
    "cut taxes", "boom", "record", "surge", "strong", "great", "winning",
    "peace", "agreement", "breakthrough", "historic",
]
BEAR_WORDS = [
    "tariff", "sanction", "war", "shutdown", "crash", "conflict", "ban",
    "recession", "inflation", "depression", "collapse", "fail", "threat",
    "danger", "attack", "retaliate", "impose", "indict", "raid",
]

# ─── Scoring & Sentiment ─────────────────────────────────────────────────────

def _score_impact(text: str) -> float:
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


# ─── Dedup & Normalisation ───────────────────────────────────────────────────

def _dedup_ok(key: str) -> bool:
    if key not in STATE["seen"]:
        return True
    t = datetime.fromisoformat(STATE["seen"][key])
    return datetime.utcnow() - t > timedelta(hours=DEDUP_HOURS)


def _norm(s: str) -> str:
    """Normalise text for dedup — keeps & so 's&p' survives keyword matching."""
    s = html.unescape(s or "").strip().lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_relevant(text: str) -> bool:
    if not MARKET_FILTER:
        return True
    t = _norm(text)
    return bool(t) and any(w in t for w in ALL_MARKET_WORDS)


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="minutes")


# ─── Source 1: trumpstruth.org RSS ───────────────────────────────────────────

def _fetch_rss() -> list:
    url = SRC_RSS
    try:
        r = requests.get(
            url, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MacroWatch/2.0)"}
        )
        r.raise_for_status()

        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            title = html.unescape(item.findtext("title") or "")
            desc  = html.unescape(item.findtext("description") or "")
            link  = item.findtext("link") or ""
            pub   = (
                item.findtext("{http://purl.org/dc/elements/1.1/}date")
                or item.findtext("pubDate") or ""
            )
            raw  = desc if len(desc) > len(title) else title
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                items.append({
                    "id": link or text[:60],
                    "text": text,
                    "url": link,
                    "ts": pub,
                    "source": "TrumpsTruth RSS",
                })

        STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
        log.info(f"RSS: {len(items)} items")
        return items

    except Exception as e:
        err = str(e)
        STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
        log.warning(f"RSS failed: {err}")
        return []


# ─── Source 2: CNN JSON archive ──────────────────────────────────────────────

def _fetch_cnn() -> list:
    url = SRC_CNN_JSON
    try:
        r = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MacroWatch/2.0)"}
        )
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list):
            raise ValueError(f"Unexpected JSON shape: {type(data)}")

        items = []
        for it in data[:CNN_RECENT_N]:
            pid  = str(it.get("id", ""))
            raw  = it.get("content") or it.get("text") or ""
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", html.unescape(text)).strip()
            post_url = it.get("url") or f"https://truthsocial.com/@realDonaldTrump/{pid}"
            ts   = it.get("created_at") or ""
            if text:
                items.append({
                    "id": pid or text[:60],
                    "text": text,
                    "url": post_url,
                    "ts": ts,
                    "source": "CNN/TruthSocial",
                })

        STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
        log.info(f"CNN JSON: {len(items)} items")
        return items

    except Exception as e:
        err = str(e)
        STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
        log.warning(f"CNN JSON failed: {err}")
        return []


# ─── Diagnostic ──────────────────────────────────────────────────────────────

def run_diag():
    lines = ["🍊 *[TrumpWatch] Diagnostic*\n"]
    if not STATE["source_health"]:
        lines.append("⚠️ No polls completed yet — check scheduler is running.")
    for url, h in STATE["source_health"].items():
        icon   = "✅" if h["ok"] else "❌"
        domain = url.split("/")[2]
        err    = f"\n   └ `{h['error'][:100]}`" if not h["ok"] else ""
        lines.append(f"{icon} `{domain}`  _(checked {h['last_checked']} UTC)_{err}")

    lines.append(f"\n📦 Buffered alerts: {len(RECENT_ALERTS)}/{RECENT_MAX}")
    lines.append(f"🔍 Dedup cache: {len(STATE['seen'])} entries")
    lines.append(f"⚙️ Threshold: {IMPACT_MIN} | Poll: {POLL_SEC}s | Market filter: {MARKET_FILTER}")
    send_text("\n".join(lines))


# ─── Recent ──────────────────────────────────────────────────────────────────

def show_recent():
    if not RECENT_ALERTS:
        send_text("🍊 [TrumpWatch] No alerts yet. Try /tw_diag to check sources.")
        return
    header = "🍊 *[TrumpWatch] Recent Alerts*\n"
    send_text(header + "\n\n────────────\n\n".join(list(RECENT_ALERTS)))


# ─── Core poll ───────────────────────────────────────────────────────────────

def poll_once():
    rss_items = _fetch_rss()
    cnn_items = _fetch_cnn()

    # Merge and cross-dedup by text fingerprint
    seen_fps: set = set()
    all_items: list = []
    for it in rss_items + cnn_items:
        fp = _norm(it["text"])[:120]
        if fp and fp not in seen_fps:
            seen_fps.add(fp)
            all_items.append(it)

    if not all_items:
        log.warning("poll_once: 0 items from all sources — check /tw_diag")
        return

    now_iso = _now_iso()
    fired   = 0

    for it in all_items[:20]:
        txt, url, src = it["text"], it["url"], it["source"]
        key = _norm(txt)[:120]

        if not _dedup_ok(key):
            continue
        STATE["seen"][key] = now_iso

        if not _is_relevant(txt):
            continue

        impact = _score_impact(txt)
        if impact < IMPACT_MIN:
            log.info(f"Below threshold ({impact}): {txt[:60]}")
            continue

        sent, emo = _sentiment(txt)
        label = "🔥 EXTREME" if impact >= 0.85 else "⚠️ HIGH" if impact >= 0.70 else "📌 MODERATE"

        msg = "\n".join(filter(None, [
            f"🍊 [TrumpWatch] {label} | Score: {impact} | {emo} {sent.title()}",
            f"🗞️ {txt.strip()[:1000]}",
            f"📡 Source: {src}",
            f"🔗 {url}" if url else "",
        ]))

        RECENT_ALERTS.appendleft(f"🕒 {now_iso} UTC\n{msg}")
        send_text(msg)
        fired += 1

    log.info(f"poll_once: {len(all_items)} checked, {fired} fired")


# ─── Entry ───────────────────────────────────────────────────────────────────

def run_loop():
    log.info(f"TrumpWatch starting — poll every {POLL_SEC}s, threshold {IMPACT_MIN}")
    while True:
        try:
            poll_once()
        except Exception as e:
            log.error(f"Unhandled error: {e}")
            try:
                send_text(f"🍊 [TrumpWatch] ⚠️ Poll crashed: {str(e)[:200]}")
            except Exception:
                pass
        time.sleep(POLL_SEC)
