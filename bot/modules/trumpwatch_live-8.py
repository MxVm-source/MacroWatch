"""
TrumpWatch Live — MacroWatch module
3-stage hybrid pipeline:
  Stage 1 — Hard block:    Pattern-match endorsements & pure political fluff
  Stage 2 — Keyword gate:  Must contain at least 1 market/macro/crypto/AI word
  Stage 3 — AI scoring:    OpenAI scores each survivor 1-10 with reasoning

Sources (confirmed working 2026):
  Primary   → trumpstruth.org/feed  (RSS, updates every ~2 min)
  Secondary → ix.cnn.io JSON        (CNN archive, updates every 5 min)
"""

import os
import json
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

# ─── Config ──────────────────────────────────────────────────────────────────

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("TW_OPENAI_MODEL", "gpt-4o-mini")   # fast + cheap
AI_SCORE_MIN    = int(os.getenv("TW_AI_SCORE_MIN", "6"))         # 0-10, fire if >=
AI_TIMEOUT      = int(os.getenv("TW_AI_TIMEOUT", "12"))

SRC_RSS         = os.getenv("TW_SOURCE_RSS",  "https://www.trumpstruth.org/feed")
SRC_CNN_JSON    = os.getenv("TW_SOURCE_CNN",  "https://ix.cnn.io/data/truth-social/truth_archive.json")

POLL_SEC        = int(os.getenv("TW_POLL_SEC",      "60"))
DEDUP_HOURS     = int(os.getenv("TW_DEDUP_HOURS",   "24"))  # raised: restarts no longer reset this
RECENT_MAX      = int(os.getenv("TW_RECENT_MAX",    "10"))
CNN_RECENT_N    = int(os.getenv("TW_CNN_RECENT_N",  "20"))

# ─── Persistent dedup ────────────────────────────────────────────────────────
# Stores seen fingerprints to disk so bot restarts never re-fire old posts.
DEDUP_FILE = os.getenv("TW_DEDUP_FILE", "/tmp/trumpwatch_seen.json")

def _load_seen() -> dict:
    try:
        with open(DEDUP_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_seen(seen: dict):
    try:
        os.makedirs(os.path.dirname(DEDUP_FILE), exist_ok=True)
        cutoff = (datetime.utcnow() - timedelta(hours=DEDUP_HOURS * 2)).isoformat()
        pruned = {k: v for k, v in seen.items() if v >= cutoff}
        with open(DEDUP_FILE, "w") as f:
            json.dump(pruned, f)
    except Exception as e:
        log.warning(f"Could not save dedup file: {e}")

RECENT_ALERTS: deque = deque(maxlen=RECENT_MAX)
STATE = {"seen": _load_seen(), "source_health": {}}
log.info(f"Loaded {len(STATE['seen'])} dedup entries from disk")

# ─── Stage 1: Hard-block patterns ────────────────────────────────────────────
# These phrases are signature boilerplate from Trump endorsement posts.
# None of them move markets. Kill them before any scoring.

BLOCK_PATTERNS = [
    r"has my complete and total endorsement",
    r"will never let you down",
    r"maga warrior",
    r"running for .{0,40}(governor|senator|comptroller|commissioner|attorney general|secretary of state|state house|state senate|congress)",
    r"endorsement (?:for re-election|to be the next)",
    r"has been with me from the (very )?beginning",
    r"i (?:fully )?endorse",
    r"vote for .{0,30}(in|on) (november|tuesday|election day|the primary)",
    r"get out and vote",
    r"make america great again\s*$",   # standalone MAGA sign-off with nothing else
]

_BLOCK_RE = [re.compile(p, re.IGNORECASE) for p in BLOCK_PATTERNS]


def _is_endorsement(text: str) -> bool:
    """Return True if post is a political endorsement / pure campaign fluff."""
    for pattern in _BLOCK_RE:
        if pattern.search(text):
            return True
    return False


# ─── Stage 2: Keyword gate ───────────────────────────────────────────────────
# Post must mention at least one of these to proceed to AI scoring.
# Broad enough to catch everything market-moving.

GATE_WORDS = {
    # Macro / economy
    "market", "markets", "stock", "stocks", "dow", "nasdaq", "s&p", "sp500",
    "wall street", "economy", "economic", "recession", "depression", "gdp",
    "inflation", "deflation", "interest rate", "fed", "federal reserve", "powell",
    "unemployment", "jobs report",
    # Fiscal / trade
    "tariff", "tariffs", "tax", "taxes", "trade deal", "trade war", "sanctions",
    "regulation", "deregulation", "deficit", "debt", "budget", "stimulus", "bailout",
    "executive order", "spending",
    # Geopolitical
    "china", "russia", "iran", "ukraine", "taiwan", "europe", "nato",
    "war", "conflict", "invasion", "nuclear", "nuke", "ceasefire",
    "oil", "gas", "opec", "energy", "middle east", "saudi",
    # Crypto / digital assets
    "bitcoin", "btc", "crypto", "cryptocurrency", "ethereum", "eth",
    "blockchain", "cbdc", "stablecoin", "digital currency", "binance", "coinbase",
    "defi", "reserve currency",
    # AI / tech policy (market-moving)
    "anthropic", "openai", "artificial intelligence", "ai regulation",
    "tech company", "silicon valley", "semiconductor", "chip", "nvidia",
    "big tech", "antitrust", "section 230",
    # Dollar / rates
    "dollar", "usd", "treasury", "bond", "yield", "rate cut", "rate hike",
    "quantitative", "liquidity",
}

def _passes_gate(text: str) -> bool:
    t = text.lower()
    # Also strip HTML before checking
    t = re.sub(r"<[^>]+>", " ", t)
    return any(w in t for w in GATE_WORDS)


# ─── Stage 3: OpenAI scoring ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional macro trader and crypto market analyst.
Your job is to assess Trump Truth Social posts for their potential market impact.

You must respond with ONLY a JSON object — no explanation, no markdown, no preamble.

JSON schema:
{
  "score": <integer 1-10>,
  "sentiment": <"bullish" | "bearish" | "neutral">,
  "affected_assets": <list of strings, e.g. ["BTC", "equities", "oil", "USD"]>,
  "reason": <one concise sentence explaining the market impact>
}

Scoring guide:
  9-10  Immediate, severe market impact (war declaration, emergency Fed action, crypto ban/embrace)
  7-8   High impact — tariffs, major sanctions, key appointments, crypto/AI policy shifts
  6     Moderate impact — geopolitical tensions, indirect fiscal signals
  4-5   Low impact — general economic commentary, vague statements
  1-3   No real market impact (endorsements, culture war, personal attacks, rallies)

Be strict. A post praising a local politician = 1-2.
A post announcing tariffs on China = 8-9.
A post mentioning bitcoin/crypto policy = 7-9 depending on specifics."""


def _ai_score(text: str) -> dict | None:
    """
    Call OpenAI to score a post. Returns dict with score/sentiment/assets/reason.
    Returns None if API key missing or call fails.
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — AI scoring disabled, using fallback")
        return None

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Post: {text[:2000]}"},
        ],
        "max_tokens": 150,
        "temperature": 0.1,   # low temp = consistent, deterministic scores
        "response_format": {"type": "json_object"},
    }

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=AI_TIMEOUT,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        result  = json.loads(content)

        # Validate required fields
        score = int(result.get("score", 0))
        if not (1 <= score <= 10):
            raise ValueError(f"Score out of range: {score}")

        return {
            "score":    score,
            "sentiment": result.get("sentiment", "neutral"),
            "assets":   result.get("affected_assets", []),
            "reason":   result.get("reason", ""),
        }

    except Exception as e:
        log.warning(f"AI scoring failed: {e}")
        return None


def _fallback_score(text: str) -> dict:
    """
    Simple keyword fallback when OpenAI is unavailable.
    More conservative than the old scorer — requires 2+ signals.
    """
    t = text.lower()
    bull = sum(1 for w in [
        "deal", "cut", "lower rates", "boom", "surge", "peace", "agreement",
        "deregulation", "stimulus", "breakthrough"
    ] if w in t)
    bear = sum(1 for w in [
        "tariff", "sanction", "war", "ban", "crash", "recession",
        "invasion", "nuclear", "impose", "retaliate", "shutdown"
    ] if w in t)

    total  = bull + bear
    score  = min(3 + total * 1.5, 8)   # caps at 8 without AI confirmation
    score  = round(score)

    sent = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    return {"score": score, "sentiment": sent, "assets": [], "reason": "Fallback scoring (AI unavailable)"}


# ─── Normalisation & Dedup ───────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="minutes")


def _norm(s: str) -> str:
    s = html.unescape(s or "").strip().lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedup_ok(key: str) -> bool:
    if key not in STATE["seen"]:
        return True
    t = datetime.fromisoformat(STATE["seen"][key])
    return datetime.utcnow() - t > timedelta(hours=DEDUP_HOURS)


# ─── Fetchers ────────────────────────────────────────────────────────────────

def _fetch_rss() -> list:
    url = SRC_RSS
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
            pub   = (item.findtext("{http://purl.org/dc/elements/1.1/}date")
                     or item.findtext("pubDate") or "")
            raw   = desc if len(desc) > len(title) else title
            text  = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if text:
                items.append({"text": text, "url": link, "ts": pub, "source": "TrumpsTruth RSS"})
        STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
        log.info(f"RSS: {len(items)} items")
        return items
    except Exception as e:
        err = str(e)
        STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
        log.warning(f"RSS failed: {err}")
        return []


def _fetch_cnn() -> list:
    url = SRC_CNN_JSON
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; MacroWatch/2.0)"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected shape: {type(data)}")
        items = []
        for it in data[:CNN_RECENT_N]:
            pid  = str(it.get("id", ""))
            raw  = it.get("content") or it.get("text") or ""
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(raw))).strip()
            url_ = it.get("url") or f"https://truthsocial.com/@realDonaldTrump/{pid}"
            if text:
                items.append({"text": text, "url": url_, "ts": it.get("created_at",""), "source": "CNN/TruthSocial"})
        STATE["source_health"][url] = {"ok": True, "last_checked": _now_iso(), "error": ""}
        log.info(f"CNN JSON: {len(items)} items")
        return items
    except Exception as e:
        err = str(e)
        STATE["source_health"][url] = {"ok": False, "last_checked": _now_iso(), "error": err}
        log.warning(f"CNN JSON failed: {err}")
        return []


# ─── Alert formatting ────────────────────────────────────────────────────────

SENTIMENT_EMOJI = {
    "bullish":  "🟢📈",
    "bearish":  "🔴📉",
    "neutral":  "🔵⚖️",
}

def _format_alert(txt: str, url: str, src: str, ai: dict) -> str:
    score   = ai["score"]
    sent    = ai["sentiment"].lower()
    assets  = ai.get("assets", [])
    reason  = ai.get("reason", "")
    emo     = SENTIMENT_EMOJI.get(sent, "🔵⚖️")

    if score >= 9:
        label = "🚨 CRITICAL"
    elif score >= 7:
        label = "🔥 HIGH"
    elif score >= 6:
        label = "⚠️ MODERATE"
    else:
        label = "📌 LOW"

    assets_line = f"💼 Assets: {', '.join(assets)}" if assets else ""
    reason_line = f"🧠 {reason}" if reason else ""

    return "\n".join(filter(None, [
        f"🍊 [TrumpWatch] {label} | Score: {score}/10 | {emo} {sent.title()}",
        assets_line,
        reason_line,
        f"🔗 {url}" if url else f"📡 {src}",
    ]))


# ─── Diagnostic ──────────────────────────────────────────────────────────────

def run_diag():
    ai_status = "✅ Configured" if OPENAI_API_KEY else "❌ OPENAI_API_KEY not set (fallback mode)"
    lines = [
        "🍊 *[TrumpWatch] Diagnostic*\n",
        f"🤖 AI Scoring: {ai_status}",
        f"📊 Model: `{OPENAI_MODEL}` | Min score to fire: {AI_SCORE_MIN}/10\n",
    ]
    for url, h in STATE["source_health"].items():
        icon   = "✅" if h["ok"] else "❌"
        domain = url.split("/")[2]
        err    = f"\n   └ `{h['error'][:100]}`" if not h["ok"] else ""
        lines.append(f"{icon} `{domain}` _(checked {h['last_checked']} UTC)_{err}")

    lines.append(f"\n📦 Buffered alerts: {len(RECENT_ALERTS)}/{RECENT_MAX}")
    lines.append(f"🔍 Dedup cache: {len(STATE['seen'])} entries")
    lines.append(f"⏱️ Poll interval: {POLL_SEC}s")
    send_text("\n".join(lines))


# ─── Recent ──────────────────────────────────────────────────────────────────

def show_recent():
    if not RECENT_ALERTS:
        send_text("🍊 [TrumpWatch] No alerts yet. Try /tw_diag to check sources.")
        return
    send_text("🍊 *[TrumpWatch] Recent Alerts*\n\n────────────\n\n"
              + "\n\n────────────\n\n".join(list(RECENT_ALERTS)))


# ─── Core poll ───────────────────────────────────────────────────────────────

def poll_once():
    rss_items = _fetch_rss()
    cnn_items = _fetch_cnn()

    # Merge + cross-dedup by text fingerprint
    seen_fps: set = set()
    all_items: list = []
    for it in rss_items + cnn_items:
        fp = _norm(it["text"])[:120]
        if fp and fp not in seen_fps:
            seen_fps.add(fp)
            all_items.append(it)

    if not all_items:
        log.warning("poll_once: 0 items — check /tw_diag")
        return

    now_iso  = _now_iso()
    fired    = 0
    blocked  = 0
    filtered = 0

    for it in all_items[:20]:
        txt, url, src = it["text"], it["url"], it["source"]
        key = _norm(txt)[:120]

        # ── Stage 1: Hard block (before dedup write — free & fast) ──
        if _is_endorsement(txt):
            blocked += 1
            log.info(f"BLOCKED (endorsement): {txt[:80]}")
            # Mark seen so we never re-process after restarts either
            STATE["seen"][key] = now_iso
            continue

        # ── Dedup check (after block, before expensive AI call) ──────
        if not _dedup_ok(key):
            continue

        # ── Stage 2: Keyword gate ────────────────────────────────────
        if not _passes_gate(txt):
            filtered += 1
            log.info(f"FILTERED (no keywords): {txt[:80]}")
            STATE["seen"][key] = now_iso
            continue

        # ── Stage 3: AI scoring ──────────────────────────────────────
        ai = _ai_score(txt) or _fallback_score(txt)
        score = ai["score"]

        log.info(f"AI score {score}/10 [{ai['sentiment']}]: {txt[:80]}")

        if score < AI_SCORE_MIN:
            log.info(f"  └ Below threshold ({AI_SCORE_MIN}) — skipped")
            STATE["seen"][key] = now_iso
            continue

        # ── Fire alert ───────────────────────────────────────────────
        STATE["seen"][key] = now_iso
        _save_seen(STATE["seen"])  # persist immediately after marking seen
        msg = _format_alert(txt, url, src, ai)
        RECENT_ALERTS.appendleft(f"🕒 {now_iso} UTC\n{msg}")
        send_text(msg)
        fired += 1

    # Persist full seen cache at end of every poll (catches blocks/filtered too)
    _save_seen(STATE["seen"])
    log.info(f"poll_once: {len(all_items)} total | {blocked} blocked | {filtered} filtered | {fired} fired")


# ─── Entry ───────────────────────────────────────────────────────────────────

def run_loop():
    log.info(f"TrumpWatch starting — poll {POLL_SEC}s | AI min score {AI_SCORE_MIN}/10 | model {OPENAI_MODEL}")
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
