"""Long-Term Relevance Engine for the INFINEX public feed.

V1 is deliberately conservative:
- TrumpWatch and FedWatch act as sensors.
- The engine scores long-term relevance for passive investors.
- Scheduled Fed events are candidates only until an outcome is known.
- Political statements are not treated as confirmed policy by default.
- Public publication is disabled by default through preview mode and the
  publisher's relevance feature flag.

The goal is to filter noise, not create trading signals.
"""

import json
import logging
import os
from datetime import datetime, timezone

import requests

from bot.public.publisher import send_public

log = logging.getLogger("public.relevance")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MIN_RELEVANCE_SCORE = int(os.getenv("PUBLIC_RELEVANCE_MIN_SCORE", "8"))
MAX_PREVIEWS = int(os.getenv("PUBLIC_RELEVANCE_MAX_PREVIEWS", "20"))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def preview_only() -> bool:
    """True by default: V1 must not auto-publish until explicitly enabled."""
    return _flag("PUBLIC_RELEVANCE_PREVIEW", True)


STATE = {
    "observed": 0,
    "eligible": 0,
    "published": 0,
    "last_observed_utc": None,
    "previews": [],
    "seen": set(),
}


STRUCTURAL_TERMS = {
    "rates": (
        "interest rate", "interest rates", "fed", "federal reserve",
        "rate cut", "rate cuts", "rate hike", "rate hikes",
    ),
    "inflation": (
        "inflation", "cpi", "ppi", "prices",
    ),
    "trade": (
        "tariff", "tariffs", "trade deal", "trade agreement",
        "import tax", "export restriction", "trade war",
    ),
    "fiscal": (
        "tax cut", "tax cuts", "tax increase", "taxes", "budget",
        "deficit", "debt ceiling", "government spending", "fiscal",
    ),
    "growth": (
        "recession", "gdp", "jobs", "employment", "unemployment",
        "economic growth",
    ),
    "regulation": (
        "regulation", "deregulation", "ban", "antitrust", "capital requirement",
    ),
    "geopolitics": (
        "sanction", "sanctions", "war", "invasion", "embargo",
    ),
}


def _fallback_driver(text: str) -> tuple[str, int]:
    """Conservative keyword fallback when AI is unavailable."""
    lowered = (text or "").lower()
    best_driver = "other"
    best_hits = 0
    for driver, terms in STRUCTURAL_TERMS.items():
        hits = sum(1 for term in terms if term in lowered)
        if hits > best_hits:
            best_driver = driver
            best_hits = hits

    if best_hits >= 3:
        score = 8
    elif best_hits == 2:
        score = 7
    elif best_hits == 1:
        score = 5
    else:
        score = 2
    return best_driver, score


def _safe_score(value) -> int:
    try:
        return max(0, min(10, int(round(float(value)))))
    except Exception:
        return 0


def _normalise_result(raw: dict, *, source_type: str, source_label: str,
                      source_url: str = "", source_text: str = "") -> dict:
    result = {
        "source_type": source_type,
        "source_label": source_label,
        "source_url": source_url or "",
        "observed_utc": datetime.now(timezone.utc),
        "relevance_score": _safe_score(raw.get("relevance_score")),
        "horizon": str(raw.get("horizon") or "short_term").lower(),
        "driver": str(raw.get("driver") or "other").lower(),
        "what_changed": str(raw.get("what_changed") or "").strip(),
        "why_it_matters": str(raw.get("why_it_matters") or "").strip(),
        "passive_takeaway": str(raw.get("passive_takeaway") or "").strip(),
        "thesis_change": str(raw.get("thesis_change") or "none").lower(),
        "confidence": str(raw.get("confidence") or "low").lower(),
        "needs_confirmation": bool(raw.get("needs_confirmation", True)),
        "source_text": (source_text or "")[:1000],
    }

    if result["horizon"] not in {"short_term", "medium_term", "structural"}:
        result["horizon"] = "short_term"
    if result["thesis_change"] not in {"none", "watch", "possible"}:
        result["thesis_change"] = "none"
    if result["confidence"] not in {"low", "medium", "high"}:
        result["confidence"] = "low"

    result["publication_eligible"] = (
        not preview_only()
        and result["relevance_score"] >= MIN_RELEVANCE_SCORE
        and result["horizon"] == "structural"
        and result["confidence"] in {"medium", "high"}
        and not result["needs_confirmation"]
    )
    return result


def _ai_evaluate_statement(text: str, source_label: str) -> dict | None:
    if not OPENAI_API_KEY:
        return None

    prompt = f"""You are a neutral long-term market relevance filter for passive investors.

Evaluate ONLY the economic and investment relevance of the supplied statement.
Do not judge the politician, party, motive, desirability, competence, or electoral impact.
Do not endorse or oppose the policy. Do not make a trading recommendation.
Do not infer facts that are not in the statement.

A high score means the item could materially change the multi-month or multi-year
investment environment through inflation, interest rates, growth, fiscal policy,
trade, regulation, or geopolitics.

A political statement or proposal is NOT confirmed policy by default.
Set needs_confirmation=true unless the supplied text itself clearly describes an
implemented/official action with enough detail to assess.

Return JSON only with:
relevance_score: integer 0-10
horizon: short_term | medium_term | structural
driver: rates | inflation | trade | fiscal | growth | regulation | geopolitics | other
what_changed: one neutral sentence
why_it_matters: one neutral sentence for a long-term investor
passive_takeaway: one sentence; usually explain that no immediate portfolio conclusion follows from one item alone
thesis_change: none | watch | possible
confidence: low | medium | high
needs_confirmation: boolean

Source label: {source_label}
Statement:
{text[:4000]}
"""

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "temperature": 0.1,
                "max_tokens": 350,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        raw = payload["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        log.warning("Long-term relevance AI evaluation failed: %s", exc)
        return None


def _fallback_statement(text: str) -> dict:
    driver, score = _fallback_driver(text)
    return {
        "relevance_score": score,
        "horizon": "structural" if score >= 8 else "medium_term" if score >= 5 else "short_term",
        "driver": driver,
        "what_changed": "A potentially market-relevant policy statement was detected.",
        "why_it_matters": (
            "It may matter if it becomes confirmed policy and changes the broader "
            f"{driver} environment."
        ),
        "passive_takeaway": (
            "This item alone is not enough to change a long-term investment thesis."
        ),
        "thesis_change": "watch" if score >= 7 else "none",
        "confidence": "low",
        "needs_confirmation": True,
    }


def _fed_candidate(event: dict) -> dict:
    category = str(event.get("category") or "").upper()
    mapping = {
        "FOMC": (8, "rates"),
        "CPI": (7, "inflation"),
        "ECB": (7, "rates"),
        "NFP": (6, "growth"),
        "PPI": (5, "inflation"),
        "SPEECH": (4, "rates"),
    }
    score, driver = mapping.get(category, (3, "other"))
    title = str(event.get("title") or category or "Macro event")

    return {
        "relevance_score": score,
        "horizon": "structural" if category in {"FOMC", "CPI", "ECB"} else "medium_term",
        "driver": driver,
        "what_changed": f"{title} is scheduled.",
        "why_it_matters": (
            "The release could affect the longer-term macro backdrop, but the scheduled "
            "event itself does not reveal the outcome."
        ),
        "passive_takeaway": (
            "Wait for the actual decision or data and the broader trend before drawing "
            "a long-term conclusion."
        ),
        "thesis_change": "watch" if score >= 7 else "none",
        "confidence": "high",
        "needs_confirmation": True,
    }


def _dedup_key(result: dict) -> str:
    source_type = result.get("source_type", "")
    url = result.get("source_url", "")
    text = result.get("source_text", "")
    changed = result.get("what_changed", "")
    base = url or text[:180] or changed[:180]
    return f"{source_type}:{base}".lower()


def _store(result: dict) -> dict:
    key = _dedup_key(result)
    if key in STATE["seen"]:
        return result

    STATE["seen"].add(key)
    STATE["observed"] += 1
    STATE["last_observed_utc"] = result["observed_utc"]
    if result["publication_eligible"]:
        STATE["eligible"] += 1

    STATE["previews"].insert(0, result)
    del STATE["previews"][MAX_PREVIEWS:]

    log.info(
        "Long-term relevance preview: source=%s score=%s horizon=%s driver=%s confirm=%s eligible=%s",
        result["source_type"],
        result["relevance_score"],
        result["horizon"],
        result["driver"],
        result["needs_confirmation"],
        result["publication_eligible"],
    )
    return result


def build_public_message(result: dict) -> str:
    """Build the eventual public format. V1 normally exposes this only as preview."""
    return "\n".join([
        "🧭 *INFINEX — LONG-TERM RELEVANCE*",
        "_MacroWatch · Passive Investor Lens_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "*WHAT CHANGED?*",
        result.get("what_changed") or "A potentially relevant development was detected.",
        "",
        "*WHY IT MATTERS LONG TERM*",
        result.get("why_it_matters") or "Long-term significance is still being assessed.",
        "",
        "*PASSIVE-INVESTOR TAKEAWAY*",
        result.get("passive_takeaway") or "No immediate long-term conclusion from this item alone.",
        "",
        f"Relevance: *{result.get('relevance_score', 0)}/10* · "
        f"Horizon: *{str(result.get('horizon', 'unknown')).replace('_', ' ').title()}*",
        "",
        "_Educational market commentary — not a trading signal._",
        "📡 INFINEX CAPITAL · MacroWatch 🧠",
    ])


def build_preview(result: dict) -> str:
    status = "ELIGIBLE" if result.get("publication_eligible") else "PREVIEW ONLY"
    confirmation = "YES" if result.get("needs_confirmation") else "NO"
    return "\n".join([
        "🧭 *Long-Term Relevance — Preview*",
        f"Status: *{status}*",
        f"Source: {result.get('source_label', '—')}",
        f"Score: *{result.get('relevance_score', 0)}/10*",
        f"Horizon: {result.get('horizon', '—')}",
        f"Driver: {result.get('driver', '—')}",
        f"Needs confirmation: *{confirmation}*",
        f"Thesis: {result.get('thesis_change', 'none')}",
        "",
        "*Public draft*",
        build_public_message(result),
    ])


def observe_trump_event(*, text: str, url: str = "", source: str = "",
                        upstream_score: int | None = None) -> dict:
    """Observe one TrumpWatch item without changing private TrumpWatch behavior."""
    raw = _ai_evaluate_statement(text, source or "TrumpWatch")
    if raw is None:
        raw = _fallback_statement(text)

    # Upstream market-impact score is context only; it never overrides relevance.
    result = _normalise_result(
        raw,
        source_type="trumpwatch",
        source_label=source or "TrumpWatch",
        source_url=url,
        source_text=text,
    )
    result["upstream_score"] = upstream_score

    _store(result)

    if result["publication_eligible"]:
        if send_public(build_public_message(result), feature="relevance"):
            STATE["published"] += 1

    return result


def observe_fed_event(*, event: dict, stage: str) -> dict | None:
    """Register major scheduled macro events as candidates, not conclusions."""
    # T-15m adds no new long-term information; avoid duplicate previews.
    if stage != "T-24h":
        return None

    raw = _fed_candidate(event)
    start = event.get("start")
    source_text = (
        f"{event.get('title', '')}|{event.get('category', '')}|"
        f"{start.isoformat() if hasattr(start, 'isoformat') else start}"
    )
    result = _normalise_result(
        raw,
        source_type="fedwatch",
        source_label=str(event.get("location") or "FedWatch"),
        source_text=source_text,
    )
    result["event_category"] = event.get("category")
    result["event_start"] = start

    return _store(result)


def latest_preview() -> str:
    if not STATE["previews"]:
        return "🧭 Long-Term Relevance: no candidate observed yet."
    return build_preview(STATE["previews"][0])


def diagnostics() -> str:
    last = STATE["last_observed_utc"]
    return "\n".join([
        "🧭 *Long-Term Relevance Engine*",
        f"Mode: *{'PREVIEW' if preview_only() else 'LIVE-CAPABLE'}*",
        f"Minimum relevance: *{MIN_RELEVANCE_SCORE}/10*",
        f"Observed: {STATE['observed']}",
        f"Eligible: {STATE['eligible']}",
        f"Published: {STATE['published']}",
        f"Last observed: {last.strftime('%Y-%m-%d %H:%M UTC') if last else '—'}",
        "",
        "Public publication also requires PUBLIC_ENABLE_RELEVANCE=true.",
    ])
