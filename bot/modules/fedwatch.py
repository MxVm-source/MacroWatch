import os, time, requests, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.utils import send_text
from bot.datafeed_bitget import get_ticker

# ---- HTML Source (FOMC Calendar) ----
FED_HTML_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# Alert offsets
ALERT_OFFSETS = [
    ("T-24h", timedelta(hours=24)),
    ("T-1h", timedelta(hours=1)),
    ("T-10m", timedelta(minutes=10)),
]

# Focus only on major Fed events
FED_EVENT_KEYWORDS = [
    "fomc meeting",
    "press conference",
    "statement",
    "minutes"
]

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")
ET_TZ = ZoneInfo("America/New_York")

# Reaction settings
REACTION_WINDOW_MIN = 10
REACTION_THRESH_PC = 0.5
BTC_SYM = "BTCUSDT_UMCBL"
ETH_SYM = "ETHUSDT_UMCBL"

STATE = {
    "events": [],
    "alert_queue": [],
    "reaction_queue": [],
    "warned": False,
    "source_ok": False,
    "last_refresh": None,
    "pre_prices": {},
}


# -------------------------------------------------------------
# Date parsing helpers
# -------------------------------------------------------------

def _fmt_brussels(dt: datetime) -> str:
    return dt.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _event_id(ev) -> str:
    return f"{ev['title']}|{ev['start'].isoformat()}"


# -------------------------------------------------------------
# HTML Parsing (no ICS anymore)
# -------------------------------------------------------------

def _fetch_html() -> str:
    try:
        r = requests.get(FED_HTML_URL, timeout=10)
        if r.ok:
            return r.text
    except Exception as e:
        print("FEDWATCH: HTML fetch failed:", e)
    return ""


def _parse_html_events(html: str):
    """
    Extract events from the FOMC calendar HTML.
    """

    events = []

    if not html:
        return events

    # Remove newlines
    text = re.sub(r"\s+", " ", html)

    # Each meeting block looks like:
    # <p><strong>March 18-19, 2025</strong>
    # Federal Open Market Committee (FOMC) Meeting</p>
    pattern = re.compile(
        r"<strong>([A-Za-z]+\s+\d{1,2}(?:–\d{1,2})?,\s+\d{4})</strong>(.*?)</p>",
        re.IGNORECASE
    )

    matches = pattern.findall(text)
    for date_str, description_block in matches:
        description = description_block.strip()

        date_str = date_str.replace("–", "-")  # Normalize dash

        if "-" in date_str:  # Multi-day meeting
            m = re.match(r"([A-Za-z]+)\s+(\d+)-(\d+),\s*(\d{4})", date_str)
            if not m:
                continue
            month, d1, d2, year = m.groups()
            day1 = f"{month} {d1}, {year}"
            day2 = f"{month} {d2}, {year}"
            dates = [day1, day2]
        else:  # Single-day event
            dates = [date_str]

        for dt_str in dates:
            try:
                base_date = datetime.strptime(dt_str, "%B %d, %Y").replace(tzinfo=ET_TZ)
            except:
                continue

            title = description.lower()

            # CLASSIFY SUBTYPES
            if "press conference" in title:
                event_time = base_date.replace(hour=14, minute=30)  # 2:30 PM ET
                final_title = "FOMC Press Conference"
            elif "statement" in title:
                event_time = base_date.replace(hour=14, minute=0)  # 2:00 PM ET
                final_title = "FOMC Statement"
            elif "meeting" in title:
                event_time = base_date.replace(hour=8, minute=0)  # Default 8 AM ET
                final_title = "FOMC Meeting"
            else:
                continue

            events.append({
                "title": final_title,
                "start": event_time.astimezone(timezone.utc),
                "location": "Federal Reserve"
            })

    return events


# -------------------------------------------------------------
# Build alert & reaction queues
# -------------------------------------------------------------

def _build_queues(events):
    alerts = []
    reactions = []
    now = datetime.now(timezone.utc)

    for ev in events:
        if ev["start"] <= now:
            continue

        ev_id = _event_id(ev)

        # Alert offsets
        for label, delta in ALERT_OFFSETS:
            when = ev["start"] - delta
            if when > now:
                alerts.append({"when": when, "label": label, "event": ev, "event_id": ev_id})

        # Reaction window
        react_when = ev["start"] + timedelta(minutes=REACTION_WINDOW_MIN)
        if react_when > now:
            reactions.append({"when": react_when, "event": ev, "event_id": ev_id})

    alerts.sort(key=lambda a: a["when"])
    reactions.sort(key=lambda a: a["when"])
    return alerts, reactions


# -------------------------------------------------------------
# Calendar refresh
# -------------------------------------------------------------

def refresh_calendar():
    html = _fetch_html()

    if not html:
        STATE["source_ok"] = False
        if not STATE["warned"]:
            send_text("🏦 [FedWatch] FOMC calendar unavailable (HTML fetch failed).")
            STATE["warned"] = True
        STATE["events"] = []
        STATE["alert_queue"] = []
        STATE["reaction_queue"] = []
        return

    STATE["source_ok"] = True
    STATE["warned"] = False

    events = _parse_html_events(html)

    # unique, sorted
    uniq = {(e["title"], e["start"]): e for e in events}
    events = sorted(uniq.values(), key=lambda e: e["start"])

    STATE["events"] = events
    alerts, reactions = _build_queues(events)
    STATE["alert_queue"] = alerts
    STATE["reaction_queue"] = reactions
    STATE["last_refresh"] = datetime.now(timezone.utc)


# -------------------------------------------------------------
# BTC/ETH reaction logic (unchanged)
# -------------------------------------------------------------

def _capture_pre_event_prices(ev_id: str):
    try:
        btc = get_ticker(BTC_SYM)
        eth = get_ticker(ETH_SYM)
        if btc is None or eth is None:
            return
        STATE["pre_prices"][ev_id] = {"btc": float(btc), "eth": float(eth)}
    except Exception as e:
        print("FEDWATCH: pre price exception:", e)


def _reaction_for_event(ev, ev_id: str):
    ref = STATE["pre_prices"].get(ev_id)
    try:
        btc_now = get_ticker(BTC_SYM)
        eth_now = get_ticker(ETH_SYM)
    except:
        return

    if not ref or btc_now is None or eth_now is None:
        return

    def pct(now, before):
        return (now - before) / before * 100 if before else 0

    btc_pc = pct(float(btc_now), ref["btc"])
    eth_pc = pct(float(eth_now), ref["eth"])

    def classify(p):
        if p >= REACTION_THRESH_PC: return "🟢 Bullish"
        if p <= -REACTION_THRESH_PC: return "🔴 Bearish"
        return "🔵 Neutral"

    msg = (
        f"🏦 [FedWatch] Market Reaction — {ev['title']}\n"
        f"🕒 {_fmt_brussels(ev['start'])}\n\n"
        f"BTC: {btc_pc:+.2f}% {classify(btc_pc)}\n"
        f"ETH: {eth_pc:+.2f}% {classify(eth_pc)}"
    )
    send_text(msg)


# -------------------------------------------------------------
# Main scheduler loop
# -------------------------------------------------------------

def schedule_loop():
    refresh_calendar()
    last_refresh = time.time()

    while True:
        now = datetime.now(timezone.utc)

        if time.time() - last_refresh > 1800:
            refresh_calendar()
            last_refresh = time.time()

        # Alerts
        if STATE["alert_queue"]:
            nxt = STATE["alert_queue"][0]
            if now >= nxt["when"]:
                ev = nxt["event"]
                send_text(
                    f"🏦 [FedWatch] Alert — {nxt['label']}\n"
                    f"🗓️ {ev['title']}\n"
                    f"🕒 {_fmt_brussels(ev['start'])}\n"
                    f"📍 Federal Reserve"
                )
                if nxt["label"] == "T-10m":
                    _capture_pre_event_prices(nxt["event_id"])
                STATE["alert_queue"].pop(0)

        # Reactions
        if STATE["reaction_queue"]:
            nxt = STATE["reaction_queue"][0]
            if now >= nxt["when"]:
                _reaction_for_event(nxt["event"], nxt["event_id"])
                STATE["reaction_queue"].pop(0)

        time.sleep(5)


# -------------------------------------------------------------
# Commands
# -------------------------------------------------------------

def show_next_event():
    if not STATE["events"]:
        refresh_calendar()
    now = datetime.now(timezone.utc)
    up = [e for e in STATE["events"] if e["start"] > now]
    if not up:
        send_text("🏦 [FedWatch] No upcoming FOMC events.")
        return
    ev = up[0]
    delta = ev["start"] - now
    h, m = divmod(int(delta.total_seconds()), 3600)
    m //= 60
    send_text(
        f"🏦 [FedWatch] Next Event\n"
        f"🗓️ {ev['title']}\n"
        f"🕒 {_fmt_brussels(ev['start'])} (in {h}h {m}m)"
    )


def show_diag(n=5):
    if not STATE["events"]:
        refresh_calendar()
    now = datetime.now(timezone.utc)
    upcoming = [e for e in STATE["events"] if e["start"] > now][:n]

    lines = [
        "🏦 [FedWatch] Diagnostics",
        f"Source: HTML FOMC Calendar",
        f"Status: {'OK' if STATE['events'] else 'NO EVENTS'}",
    ]

    if not upcoming:
        lines.append("No upcoming events.")
    else:
        lines.append("")
        lines.append("Next events:")
        for ev in upcoming:
            lines.append(f"• {ev['title']} – {_fmt_brussels(ev['start'])}")

    send_text("\n".join(lines))