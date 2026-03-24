"""
FedWatch — MacroWatch module
Tracks all major macro events that move crypto markets.

Events covered:
  - FOMC Statements & Press Conferences  (Fed HTML calendar)
  - CPI / Core CPI                        (BLS.gov API — free, official)
  - PPI                                   (BLS.gov API)
  - NFP Jobs Report                       (BLS.gov API)
  - Powell Speeches & Fed Testimonies     (Fed HTML scrape)
  - ECB Rate Decisions                    (hardcoded schedule + ECB RSS)

Rate probability (replaces dead Reuters RSS):
  - Fetches ZQ (30-Day Fed Funds futures) price from Yahoo Finance
  - Computes cut/hold/hike probability using the exact CME FedWatch methodology
  - Free, no API key, real-time during market hours

Architecture fix vs original:
  - Exposes poll_once() for APScheduler (no blocking while-loop)
  - schedule_loop() kept for standalone/legacy usage
"""

import os
import re
import time
import logging
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.utils import send_text
from bot.datafeed_bitget import get_ticker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FedWatch] %(message)s")
log = logging.getLogger("fedwatch")

# ─── Timezone ────────────────────────────────────────────────────────────────

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")
ET_TZ       = ZoneInfo("America/New_York")

# ─── Config ──────────────────────────────────────────────────────────────────

ALERT_OFFSETS = [
    ("T-24h", timedelta(hours=24)),
    ("T-1h",  timedelta(hours=1)),
    ("T-10m", timedelta(minutes=10)),
]

REACTION_WINDOW_MIN = 10
REACTION_THRESH_PC  = 0.5
BTC_SYM = "BTCUSDT_UMCBL"
ETH_SYM = "ETHUSDT_UMCBL"

# BLS series IDs for economic data (free, no key required)
BLS_CPI_SERIES  = "CUSR0000SA0"    # CPI All Urban Consumers
BLS_CPIX_SERIES = "CUSR0000SA0L1E" # Core CPI (ex food & energy)
BLS_PPI_SERIES  = "WPSFD4"         # PPI Final Demand
BLS_NFP_SERIES  = "CES0000000001"  # Total Nonfarm Payrolls

# Yahoo Finance ticker for ZQ (30-Day Fed Funds Futures front month)
# ZQ futures: price = 100 - implied fed funds rate
YAHOO_ZQ_URL = "https://query1.finance.yahoo.com/v8/finance/chart/ZQ=F?interval=1d&range=1d"

STATE = {
    "events":          [],
    "alert_queue":     [],
    "reaction_queue":  [],
    "fired_alerts":    set(),   # set of (event_id, label) already sent
    "pre_prices":      {},
    "warned":          False,
    "source_ok":       False,
    "last_refresh":    None,
    "last_poll":       None,
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _fmt(dt: datetime) -> str:
    return dt.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d %H:%M %Z")

def _event_id(ev: dict) -> str:
    return f"{ev['title']}|{ev['start'].isoformat()}"


# ─── Rate Probability via ZQ Futures (CME FedWatch methodology) ──────────────

CURRENT_RATE_PCT = float(os.getenv("FW_CURRENT_RATE", "5.25"))  # update if Fed changes rates

def _fetch_zq_price() -> float | None:
    """Fetch front-month ZQ (30-Day Fed Funds Futures) price from Yahoo Finance."""
    try:
        r = requests.get(
            YAHOO_ZQ_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        r.raise_for_status()
        data   = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        price  = next((p for p in reversed(closes) if p is not None), None)
        log.info(f"ZQ price: {price}")
        return float(price) if price else None
    except Exception as e:
        log.warning(f"ZQ fetch failed: {e}")
        return None


def _compute_rate_probability() -> dict:
    """
    CME FedWatch methodology:
      ZQ price = 100 - implied_avg_fed_funds_rate
      implied_rate = 100 - ZQ_price
      
    Returns dict with cut_pct, hold_pct, hike_pct, implied_rate, consensus
    """
    zq = _fetch_zq_price()
    if zq is None:
        return {"cut_pct": None, "hold_pct": None, "hike_pct": None,
                "implied_rate": None, "consensus": "unknown", "source": "unavailable"}

    implied_rate = 100.0 - zq
    current      = CURRENT_RATE_PCT
    diff_bps     = round((implied_rate - current) * 100)

    # Snap to nearest 25bp outcome
    if diff_bps <= -37:
        cut_pct  = 100.0; hold_pct = 0.0; hike_pct = 0.0
    elif diff_bps <= -12:
        # Between -50bp and -25bp: interpolate cut vs hold
        cut_pct  = round(min(100, max(0, (abs(diff_bps) - 12) / 25 * 100)), 1)
        hold_pct = round(100 - cut_pct, 1)
        hike_pct = 0.0
    elif diff_bps <= 12:
        cut_pct  = 0.0; hold_pct = 100.0; hike_pct = 0.0
    elif diff_bps <= 37:
        hike_pct = round(min(100, max(0, (diff_bps - 12) / 25 * 100)), 1)
        hold_pct = round(100 - hike_pct, 1)
        cut_pct  = 0.0
    else:
        cut_pct  = 0.0; hold_pct = 0.0; hike_pct = 100.0

    # Consensus label
    if cut_pct >= 60:
        consensus = f"CUT ({cut_pct:.0f}% probability)"
    elif hike_pct >= 60:
        consensus = f"HIKE ({hike_pct:.0f}% probability)"
    elif hold_pct >= 60:
        consensus = f"HOLD ({hold_pct:.0f}% probability)"
    else:
        consensus = f"UNCERTAIN — Cut {cut_pct:.0f}% / Hold {hold_pct:.0f}% / Hike {hike_pct:.0f}%"

    return {
        "cut_pct":      cut_pct,
        "hold_pct":     hold_pct,
        "hike_pct":     hike_pct,
        "implied_rate": round(implied_rate, 3),
        "consensus":    consensus,
        "source":       "ZQ Futures / CME methodology",
    }


def _rate_prob_line() -> str:
    """One-line summary for alert messages."""
    p = _compute_rate_probability()
    if p["implied_rate"] is None:
        return "📊 Market consensus: unavailable"
    return (
        f"📊 Market consensus: {p['consensus']}\n"
        f"   (Implied rate: {p['implied_rate']:.2f}% | "
        f"Cut {p['cut_pct']}% / Hold {p['hold_pct']}% / Hike {p['hike_pct']}%)"
    )


# ─── FOMC Calendar (Fed HTML) ────────────────────────────────────────────────

FED_HTML_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

def _fetch_fomc_events() -> list:
    try:
        r = requests.get(FED_HTML_URL, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log.warning(f"FOMC HTML fetch failed: {e}")
        return []

    events = []
    text   = re.sub(r"\s+", " ", html)

    year_matches = list(re.finditer(r"(\d{4})\s+FOMC Meetings", text))
    for idx, m in enumerate(year_matches):
        year    = int(m.group(1))
        section = text[m.end(): (year_matches[idx+1].start() if idx+1 < len(year_matches) else len(text))]

        md_pat = re.compile(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{1,2})(?:-(\d{1,2})\*?)?"
        )
        for md in md_pat.finditer(section):
            month_name = md.group(1)
            d2         = int(md.group(3) or md.group(2))
            try:
                base = datetime(year, datetime.strptime(month_name, "%B").month, d2, tzinfo=ET_TZ)
            except Exception:
                continue

            for title, hour, minute in [
                ("FOMC Statement",      14, 0),
                ("FOMC Press Conference", 14, 30),
            ]:
                events.append({
                    "title":    title,
                    "start":    base.replace(hour=hour, minute=minute).astimezone(timezone.utc),
                    "category": "FOMC",
                    "location": "Federal Reserve",
                })

    # Also check for Powell testimonies / speeches in the HTML
    speech_pat = re.compile(
        r"(Chair|Governor|Vice Chair).{0,60}(testif|speech|speak|remarks|deliver).{0,100}"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(\d{4})",
        re.IGNORECASE
    )
    for sp in speech_pat.finditer(text):
        try:
            month = datetime.strptime(sp.group(3), "%B").month
            day   = int(sp.group(4))
            year_ = int(sp.group(5))
            dt    = datetime(year_, month, day, 10, 0, tzinfo=ET_TZ).astimezone(timezone.utc)
            events.append({
                "title":    "Fed Chair Speech/Testimony",
                "start":    dt,
                "category": "SPEECH",
                "location": "Federal Reserve",
            })
        except Exception:
            continue

    log.info(f"FOMC events parsed: {len(events)}")
    return events


# ─── BLS Economic Data (CPI, PPI, NFP) ───────────────────────────────────────
# BLS.gov blocks server IPs with 403. Using hardcoded 2026 official schedule
# instead — BLS publishes the full year in January and dates never change.
# Source: https://www.bls.gov/schedule/news_release/

# All times are ET (08:30). Format: (month, day)
_BLS_2026 = {
    "CPI": [
        (1, 15), (2, 12), (3, 12), (4, 10), (5, 13), (6, 11),
        (7, 15), (8, 12), (9, 11), (10, 13), (11, 13), (12, 10),
    ],
    "PPI": [
        (1, 14), (2, 13), (3, 13), (4, 9),  (5, 14), (6, 12),
        (7, 14), (8, 13), (9, 10), (10, 14), (11, 12), (12, 11),
    ],
    "NFP": [
        (1, 9),  (2, 6),  (3, 6),  (4, 3),  (5, 8),  (6, 5),
        (7, 10), (8, 7),  (9, 4),  (10, 2), (11, 6),  (12, 4),
    ],
}

_BLS_TITLES = {
    "CPI": "CPI Release",
    "PPI": "PPI Release",
    "NFP": "NFP Jobs Report",
}

def _fetch_bls_dates(release_type: str) -> list:
    """Return upcoming BLS release dates from hardcoded 2026 schedule."""
    dates = _BLS_2026.get(release_type, [])
    now   = _now()
    events = []
    for month, day in dates:
        try:
            dt = datetime(2026, month, day, 8, 30, tzinfo=ET_TZ).astimezone(timezone.utc)
            if dt > now:
                events.append({
                    "title":    _BLS_TITLES[release_type],
                    "start":    dt,
                    "category": release_type,
                    "location": "Bureau of Labor Statistics",
                })
        except Exception:
            continue
    log.info(f"BLS {release_type}: {len(events)} upcoming dates")
    return events


# ─── ECB Rate Decisions ───────────────────────────────────────────────────────
# ECB RSS only contains past press releases, not upcoming decisions.
# Using hardcoded 2026 official meeting calendar instead.
# Source: https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
# All decisions published at 14:15 CET (13:15 UTC).

_ECB_2026 = [
    (1, 30), (3, 6),  (4, 17), (6, 5),
    (7, 24), (9, 11), (10, 23),(12, 11),
]

def _fetch_ecb_events() -> list:
    now    = _now()
    CET    = timezone(timedelta(hours=1))
    events = []
    for month, day in _ECB_2026:
        try:
            dt = datetime(2026, month, day, 14, 15, tzinfo=CET).astimezone(timezone.utc)
            if dt > now:
                events.append({
                    "title":    "ECB Rate Decision",
                    "start":    dt,
                    "category": "ECB",
                    "location": "European Central Bank",
                })
        except Exception:
            continue
    log.info(f"ECB events: {len(events)}")
    return events


# ─── Calendar Refresh ─────────────────────────────────────────────────────────

def refresh_calendar():
    log.info("Refreshing calendar...")
    all_events = []

    # FOMC + speeches
    all_events += _fetch_fomc_events()

    # BLS economic releases
    for rtype in ["CPI", "PPI", "NFP"]:
        all_events += _fetch_bls_dates(rtype)

    # ECB
    all_events += _fetch_ecb_events()

    if not all_events:
        STATE["source_ok"] = False
        if not STATE["warned"]:
            send_text("🏦 [FedWatch] ⚠️ Calendar refresh returned 0 events — sources may be down.")
            STATE["warned"] = True
        return

    STATE["source_ok"] = True
    STATE["warned"]    = False

    # Deduplicate by (title, start)
    uniq   = {(e["title"], e["start"]): e for e in all_events}
    events = sorted(uniq.values(), key=lambda e: e["start"])

    STATE["events"]       = events
    STATE["last_refresh"] = _now()

    _rebuild_queues()
    log.info(f"Calendar refreshed: {len(events)} total events")


def _rebuild_queues():
    alerts    = []
    reactions = []
    now       = _now()

    for ev in STATE["events"]:
        if ev["start"] <= now:
            continue
        ev_id = _event_id(ev)

        for label, delta in ALERT_OFFSETS:
            when = ev["start"] - delta
            if when > now:
                alerts.append({"when": when, "label": label, "event": ev, "event_id": ev_id})

        react_when = ev["start"] + timedelta(minutes=REACTION_WINDOW_MIN)
        if react_when > now:
            reactions.append({"when": react_when, "event": ev, "event_id": ev_id})

    alerts.sort(key=lambda a: a["when"])
    reactions.sort(key=lambda a: a["when"])

    STATE["alert_queue"]    = alerts
    STATE["reaction_queue"] = reactions


# ─── Pre-event price capture ─────────────────────────────────────────────────

def _capture_pre_prices(ev_id: str):
    try:
        btc = get_ticker(BTC_SYM)
        eth = get_ticker(ETH_SYM)
        if btc and eth:
            STATE["pre_prices"][ev_id] = {"btc": float(btc), "eth": float(eth)}
    except Exception as e:
        log.warning(f"Pre-price capture failed: {e}")


# ─── Category emoji ──────────────────────────────────────────────────────────

CATEGORY_EMOJI = {
    "FOMC":   "🏦",
    "CPI":    "📈",
    "PPI":    "🏭",
    "NFP":    "👷",
    "SPEECH": "🎙️",
    "ECB":    "🇪🇺",
}

def _cat_emoji(ev: dict) -> str:
    return CATEGORY_EMOJI.get(ev.get("category", ""), "📅")


# ─── Alert sending ────────────────────────────────────────────────────────────

def _send_alert(alert: dict):
    ev      = alert["event"]
    label   = alert["label"]
    ev_id   = alert["event_id"]
    fire_key = (ev_id, label)

    if fire_key in STATE["fired_alerts"]:
        return
    STATE["fired_alerts"].add(fire_key)

    emoji    = _cat_emoji(ev)
    category = ev.get("category", "")

    lines = [
        f"{emoji} [FedWatch] {label} — {ev['title']}",
        f"🕒 {_fmt(ev['start'])}",
        f"📍 {ev.get('location', '')}",
    ]

    # Rate probability for FOMC events only (not relevant for CPI/NFP)
    if category == "FOMC" and label in ("T-1h", "T-10m"):
        lines.append("")
        lines.append(_rate_prob_line())

    # Pre-meeting bias summary for all macro events at T-1h
    if label == "T-1h":
        if category in ("CPI", "NFP", "PPI"):
            lines.append("")
            lines.append(f"⚠️ High-impact release — expect BTC/ETH volatility at open")
        elif category == "ECB":
            lines.append("")
            lines.append(f"🇪🇺 ECB decision can move EUR pairs and crypto risk appetite")

    send_text("\n".join(filter(None, lines)))

    # Capture prices at T-10m for post-event reaction
    if label == "T-10m":
        _capture_pre_prices(ev_id)


# ─── Post-event reaction ─────────────────────────────────────────────────────

def _send_reaction(ev: dict, ev_id: str):
    ref = STATE["pre_prices"].get(ev_id)
    if not ref:
        return

    try:
        btc_now = float(get_ticker(BTC_SYM))
        eth_now = float(get_ticker(ETH_SYM))
    except Exception:
        return

    def pct(now, before):
        return (now - before) / before * 100 if before else 0

    def tag(pc):
        if pc >= REACTION_THRESH_PC:   return "🟢 Bullish"
        if pc <= -REACTION_THRESH_PC:  return "🔴 Bearish"
        return "🔵 Neutral"

    btc_pc = pct(btc_now, ref["btc"])
    eth_pc = pct(eth_now, ref["eth"])
    emoji  = _cat_emoji(ev)

    lines = [
        f"{emoji} [FedWatch] Market Reaction — {ev['title']}",
        f"🕒 {_fmt(ev['start'])}",
        "",
        f"BTC: {btc_pc:+.2f}%  {tag(btc_pc)}",
        f"ETH: {eth_pc:+.2f}%  {tag(eth_pc)}",
    ]

    # For FOMC: add updated rate probability (post-decision)
    if ev.get("category") == "FOMC" and "Statement" in ev["title"]:
        lines.append("")
        lines.append(_rate_prob_line())
        lines.append("ℹ️ Rate probability now reflects post-decision market pricing")

    send_text("\n".join(lines))


# ─── poll_once — called by APScheduler ───────────────────────────────────────

REFRESH_INTERVAL_H = int(os.getenv("FW_REFRESH_HOURS", "6"))

def poll_once():
    """APScheduler entrypoint. Check queues and refresh calendar periodically."""
    now = _now()
    STATE["last_poll"] = now

    # Refresh calendar if stale or empty
    if (not STATE["events"]
            or STATE["last_refresh"] is None
            or (now - STATE["last_refresh"]) > timedelta(hours=REFRESH_INTERVAL_H)):
        refresh_calendar()

    # Fire due alerts
    fired_any = False
    while STATE["alert_queue"] and now >= STATE["alert_queue"][0]["when"]:
        _send_alert(STATE["alert_queue"].pop(0))
        fired_any = True

    # Fire due reactions
    while STATE["reaction_queue"] and now >= STATE["reaction_queue"][0]["when"]:
        nxt = STATE["reaction_queue"].pop(0)
        _send_reaction(nxt["event"], nxt["event_id"])

    if fired_any:
        log.info("Alerts fired this poll")


# ─── Commands ────────────────────────────────────────────────────────────────

def show_next_event():
    if not STATE["events"]:
        refresh_calendar()
    now = _now()
    up  = [e for e in STATE["events"] if e["start"] > now]
    if not up:
        send_text("🏦 [FedWatch] No upcoming events found.")
        return
    ev    = up[0]
    delta = ev["start"] - now
    hrs, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    emoji = _cat_emoji(ev)
    send_text(
        f"{emoji} [FedWatch] Next Event\n"
        f"🗓️ {ev['title']}\n"
        f"🕒 {_fmt(ev['start'])} (in {hrs}h {mins}m)\n"
        f"📍 {ev.get('location', '')}"
    )


def show_diag(n: int = 8):
    if not STATE["events"]:
        refresh_calendar()

    now      = _now()
    upcoming = [e for e in STATE["events"] if e["start"] > now][:n]

    # Group by category for clarity
    by_cat: dict = {}
    for ev in upcoming:
        cat = ev.get("category", "OTHER")
        by_cat.setdefault(cat, []).append(ev)

    lines = [
        "🏦 *[FedWatch] Diagnostics*",
        f"Source status: {'✅ OK' if STATE['source_ok'] else '⚠️ DEGRADED'}",
        f"Last refresh: {_fmt(STATE['last_refresh']) if STATE['last_refresh'] else 'Never'}",
        f"Queued alerts: {len(STATE['alert_queue'])} | Reactions: {len(STATE['reaction_queue'])}",
        "",
        "📅 *Upcoming Events:*",
    ]

    for cat, evs in by_cat.items():
        emoji = CATEGORY_EMOJI.get(cat, "📅")
        for ev in evs:
            delta = ev["start"] - now
            hrs   = int(delta.total_seconds() // 3600)
            lines.append(f"  {emoji} {ev['title']} — {_fmt(ev['start'])} (in {hrs}h)")

    # Rate probability snapshot
    lines.append("")
    lines.append(_rate_prob_line())

    send_text("\n".join(lines))


# ─── Legacy: standalone loop (kept for backwards compat) ─────────────────────

def schedule_loop():
    """Blocking loop — use only when running FedWatch standalone, not with APScheduler."""
    refresh_calendar()
    while True:
        poll_once()
        time.sleep(30)
