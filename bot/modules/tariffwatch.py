# bot/modules/tariffwatch.py
"""
TariffWatch — Dedicated trade war & tariff intelligence module.

Monitors multiple sources for tariff announcements, trade policy changes,
and trade war escalations. Scores impact on crypto markets via OpenAI.

Sources:
  - Reuters Business RSS
  - Politico Trade RSS
  - TrumpsTruth RSS (tariff-specific filter, separate from TrumpWatch)

Fires only when content is tariff/trade-war relevant and AI score >= threshold.
Deduplicates by URL via Upstash Redis — same infrastructure as TrumpWatch.
"""

import html
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("tariffwatch")

# ─── Config ──────────────────────────────────────────────────────────────────

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("TW_OPENAI_MODEL", "gpt-4o-mini")
AI_SCORE_MIN    = int(os.getenv("TARIFF_AI_SCORE_MIN", "6"))
AI_TIMEOUT      = int(os.getenv("TARIFF_AI_TIMEOUT", "12"))
RECENT_MAX      = int(os.getenv("TARIFF_RECENT_MAX", "10"))
DEDUP_HOURS     = int(os.getenv("TARIFF_DEDUP_HOURS", "720"))  # 30 days

UPSTASH_URL     = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN   = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_PREFIX    = "tariff_seen:"

# Sources
SRC_REUTERS     = os.getenv("TARIFF_REUTERS_RSS",  "https://feeds.reuters.com/reuters/businessNews")
SRC_POLITICO    = os.getenv("TARIFF_POLITICO_RSS", "https://rss.politico.com/economy.xml")
SRC_TRUMP_RSS   = os.getenv("TW_SOURCE_RSS",       "https://www.trumpstruth.org/feed")

# ─── Tariff keywords ─────────────────────────────────────────────────────────

TARIFF_KEYWORDS = {
    # Direct tariff terms
    "tariff", "tariffs", "import duty", "import duties", "customs duty",
    "trade war", "trade deal", "trade deficit", "trade surplus",
    "section 301", "section 232", "section 201",
    # Countries in trade conflict
    "trade with china", "chinese goods", "chinese imports",
    "trade with eu", "european imports",
    "trade with canada", "canadian goods",
    "trade with mexico", "mexican goods",
    # Actions
    "impose tariff", "slap tariff", "announce tariff", "tariff increase",
    "tariff reduction", "tariff exemption", "tariff waiver",
    "trade sanction", "trade restriction", "trade barrier",
    "retaliatory tariff", "counter-tariff", "trade retaliation",
    "wto ruling", "trade dispute", "trade negotiation",
    "trade representative", "ustr",
    # Sectors
    "steel tariff", "aluminum tariff", "semiconductor tariff",
    "auto tariff", "chip tariff", "solar tariff", "ev tariff",
}

# ─── State ───────────────────────────────────────────────────────────────────

RECENT_ALERTS: deque = deque(maxlen=RECENT_MAX)
_MEM_SEEN: dict = {}

STATE = {
    "seen":          _MEM_SEEN,
    "source_health": {},
    "last_check_utc": None,
    "total_fired":   0,
}

# ─── Redis helpers ────────────────────────────────────────────────────────────

def _redis_available() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)

def _redis_headers() -> dict:
    return {"Authorization": f"Bearer {UPSTASH_TOKEN}"}

def _redis_set(key: str) -> bool:
    if not _redis_available():
        return False
    try:
        r = requests.post(
            f"{UPSTASH_URL}/set/{REDIS_PREFIX}{key}/1/ex/{DEDUP_HOURS * 3600}",
            headers=_redis_headers(), timeout=5,
        )
        return r.ok
    except Exception as e:
        log.warning(f"Redis SET failed: {e}")
        return False

def _redis_exists(key: str) -> bool:
    if not _redis_available():
        return False
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{REDIS_PREFIX}{key}",
            headers=_redis_headers(), timeout=5,
        )
        return r.ok and r.json().get("result") is not None
    except Exception:
        return False

def _dedup_check(key: str) -> bool:
    if key in _MEM_SEEN:
        return False
    if _redis_exists(key):
        _MEM_SEEN[key] = _now_iso()
        return False
    return True

def _dedup_mark(key: str):
    _MEM_SEEN[key] = _now_iso()
    _redis_set(key)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="minutes")

def _url_key(url: str) -> str:
    m = re.search(r"/(\d{4,})/?$", (url or "").split("?")[0])
    if m:
        return f"tariff_post:{m.group(1)}"
    return re.sub(r"[?#].*", "", url).rstrip("/").lower()

def _norm(s: str) -> str:
    s = html.unescape(s or "").strip().lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _passes_tariff_gate(text: str) -> bool:
    """Returns True if text contains at least one tariff-related keyword."""
    t = text.lower()
    return any(kw in t for kw in TARIFF_KEYWORDS)

# ─── Fetchers ─────────────────────────────────────────────────────────────────

def _fetch_rss(url: str, source_name: str) -> list:
    try:
        r = requests.get(url, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; MacroWatch/2.0)"})
        r.raise_for_status()
        root  = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            title = html.unescape(item.findtext("title") or "")
            desc  = html.unescape(item.findtext("description") or "")
            link  = item.findtext("link") or ""
            raw   = desc if len(desc) > len(title) else title
            text  = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if text and len(text) >= 30:
                items.append({"text": text, "url": link, "source": source_name})
        STATE["source_health"][source_name] = {"ok": True, "last_checked": _now_iso()}
        log.info(f"{source_name}: {len(items)} items")
        return items
    except Exception as e:
        STATE["source_health"][source_name] = {"ok": False, "last_checked": _now_iso(), "error": str(e)}
        log.warning(f"{source_name} fetch failed: {e}")
        return []

# ─── AI scoring ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a trade policy analyst assessing tariff/trade war news for crypto market impact.

Score the market impact of this tariff/trade news on crypto markets (1-10):
9-10: Major tariff announcement (25%+ on key sector), trade war escalation, WTO ruling
7-8: New tariffs announced, significant trade deal, major retaliation
6: Trade policy signal, tariff threat, negotiation update
4-5: Trade commentary, minor adjustment
1-3: Routine trade statistics, background noise

Respond ONLY with valid JSON:
{"score": <1-10>, "sentiment": "bullish|bearish|neutral", "affected": ["BTC", "equities", "semiconductors"], "reason": "<one line>", "countries": ["US", "China"]}"""

def _ai_score(text: str) -> dict | None:
    if not OPENAI_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 150,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": f"Score this trade/tariff news:\n\n{text[:800]}"},
                ],
            },
            timeout=AI_TIMEOUT,
        )
        r.raise_for_status()
        result = json.loads(r.json()["choices"][0]["message"]["content"])
        score  = int(result.get("score", 0))
        if not (1 <= score <= 10):
            raise ValueError(f"Score out of range: {score}")
        return {
            "score":     score,
            "sentiment": result.get("sentiment", "neutral"),
            "affected":  result.get("affected", []),
            "reason":    result.get("reason", ""),
            "countries": result.get("countries", []),
        }
    except Exception as e:
        log.warning(f"AI scoring failed: {e}")
        return None

# ─── Alert format ─────────────────────────────────────────────────────────────

SENTIMENT_EMOJI = {
    "bullish": "🟢📈",
    "bearish": "🔴📉",
    "neutral": "🔵⚖️",
}

def _format_alert(text: str, url: str, src: str, ai: dict) -> str:
    score = ai["score"]
    sent  = ai["sentiment"].lower()
    emo   = SENTIMENT_EMOJI.get(sent, "🔵⚖️")

    if score >= 9:
        label = "🚨 CRITICAL"
    elif score >= 7:
        label = "🔥 HIGH"
    else:
        label = "⚠️ MODERATE"

    countries = " • ".join(ai.get("countries", [])) if ai.get("countries") else ""
    affected  = ", ".join(ai.get("affected", [])) if ai.get("affected") else ""

    lines = [f"🌐 [TariffWatch] {label} | Score: {score}/10 | {emo} {sent.title()}"]
    if countries:
        lines.append(f"🗺️ {countries}")
    if affected:
        lines.append(f"💼 {affected}")
    if ai.get("reason"):
        lines.append(f"🧠 {ai['reason']}")
    lines.append(f"🔗 {url}" if url else f"📡 {src}")

    return "\n".join(lines)

# ─── Core poll ────────────────────────────────────────────────────────────────

def poll_once():
    STATE["last_check_utc"] = datetime.now(timezone.utc)

    # Fetch from all sources
    all_items = []
    seen_fps: set = set()

    for url, name in [
        (SRC_REUTERS,  "Reuters"),
        (SRC_POLITICO, "Politico"),
        (SRC_TRUMP_RSS,"TrumpsTruth"),
    ]:
        for it in _fetch_rss(url, name):
            url_k  = _url_key(it.get("url", ""))
            text_k = _norm(it["text"])[:120]
            fp     = url_k if url_k else text_k
            if fp and fp not in seen_fps:
                seen_fps.add(fp)
                all_items.append(it)

    fired = filtered = 0

    for it in all_items[:30]:
        txt, url, src = it["text"], it["url"], it["source"]
        key = _url_key(url) if url else _norm(txt)[:120]

        # Dedup
        if not _dedup_check(key):
            continue

        # Keyword gate — must be tariff related
        if not _passes_tariff_gate(txt):
            filtered += 1
            _dedup_mark(key)
            continue

        # AI score
        ai = _ai_score(txt)
        if not ai:
            _dedup_mark(key)
            continue

        if ai["score"] < AI_SCORE_MIN:
            log.debug(f"TariffWatch below threshold ({ai['score']}): {txt[:60]}")
            _dedup_mark(key)
            continue

        # Fire
        _dedup_mark(key)
        msg = _format_alert(txt, url, src, ai)
        RECENT_ALERTS.appendleft(f"🕒 {_now_iso()} UTC\n{msg}")
        send_text(msg)
        STATE["total_fired"] += 1
        fired += 1

    log.info(f"TariffWatch poll: {len(all_items)} total | {filtered} filtered | {fired} fired")


def show_recent():
    if not RECENT_ALERTS:
        send_text("🌐 [TariffWatch] No alerts yet.")
        return
    send_text("🌐 *[TariffWatch] Recent Alerts*\n\n────────────\n\n"
              + "\n\n────────────\n\n".join(list(RECENT_ALERTS)))


def show_diag():
    lines = [
        "🌐 *[TariffWatch] Diagnostic*\n",
        f"🤖 AI: {'✅' if OPENAI_API_KEY else '❌ Not configured'}",
        f"💾 Redis: {'✅ Connected' if _redis_available() else '⚠️ In-memory only'}",
        f"📦 Recent alerts: {len(RECENT_ALERTS)}/{RECENT_MAX}",
        f"🔥 Total fired: {STATE['total_fired']}",
        f"⏱️ Last check: {STATE['last_check_utc'].strftime('%H:%M UTC') if STATE['last_check_utc'] else '—'}\n",
    ]
    for name, h in STATE["source_health"].items():
        icon = "✅" if h.get("ok") else "❌"
        err  = f" — {h.get('error','')[:60]}" if not h.get("ok") else ""
        lines.append(f"{icon} {name}{err}")
    send_text("\n".join(lines))
