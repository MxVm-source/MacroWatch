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
# Time Formatting
# -------------------------------------------------------------

def _fmt_brussels(dt: datetime) -> str:
    return dt.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _event_id(ev) -> str:
    return f"{ev['title']}|{ev['start'].isoformat()}"


# -------------------------------------------------------------
# HTML Fetch
# -------------------------------------------------------------

def _fetch_html() -> str:
    try:
        r = requests.get(FED_HTML_URL, timeout=10)
        if r.ok:
            return r.text
    except Exception as e:
        print("FEDWATCH: HTML fetch failed:", e)
    return ""


# -------------------------------------------------------------
# HTML Parsing (FOMC + related events)
# -------------------------------------------------------------

def _parse_html_events(html: str):
    """
    Extract events from the FOMC HTML calendar.

    We classify:
    - FOMC Meeting
    - FOMC Statement
    - FOMC Press Conference
    - FOMC Minutes
    - Powell Testimony — Monetary Policy Report (if present in text)
    """
    events = []
    if not html:
        return events

    # Remove whitespace
    text = re.sub(r"\s+", " ", html)

    # Pattern for each FOMC block
    pattern = re.compile(
        r"<strong>([A-Za-z]+\s+\d{1,2}(?:–\d{1,2})?,\s+\d{4})</strong>(.*?)</p>",
        re.IGNORECASE
    )

    matches = pattern.findall(text)

    for date_str, description in matches:
        # Normalized lower text for classification
        desc_lower = description.strip().lower()
        date_str = date_str.replace("–", "-")

        # multi-day meetings
        if "-" in date_str:
            m = re.match(r"([A-Za-z]+)\s+(\d+)-(\d+),\s*(\d{4})", date_str)
            if not m:
                continue
            month, d1, d2, year = m.groups()
            dates = [
                f"{month} {d1}, {year}",
                f"{month} {d2}, {year}"
            ]
        else:
            dates = [date_str]

        for dt_str in dates:
            try:
                base_date = datetime.strptime(dt_str, "%B %d, %Y").replace(tzinfo=ET_TZ)
            except Exception:
                continue

            # ---- Classification ----
            title = None
            event_time = None
            location = "Federal Reserve"

            # Priority order matters; minutes/testimony must not be swallowed
            if "minutes" in desc_lower:
                # FOMC Minutes – usually 14:00 ET
                event_time = base_date.replace(hour=14, minute=0)
                title = "FOMC Minutes"
            elif "press conference" in desc_lower:
                event_time = base_date.replace(hour=14, minute=30)
                title = "FOMC Press Conference"
            elif "statement" in desc_lower:
                event_time = base_date.replace(hour=14, minute=0)
                title = "FOMC Statement"
            elif "meeting" in desc_lower:
                event_time = base_date.replace(hour=8, minute=0)
                title = "FOMC Meeting"
            elif "monetary policy report" in desc_lower or "semiannual monetary policy report" in desc_lower:
                # Powell Testimony — Monetary Policy Report (Humphrey-Hawkins)
                # Time on site may differ, but we approximate 10:00 ET.
                event_time = base_date.replace(hour=10, minute=0)
                title = "Powell Testimony — Monetary Policy Report"
                location = "U.S. Congress"

            if not title or not event_time:
                continue

            events.append(
                {
                    "title": title,
                    "start": event_time.astimezone(timezone.utc),
                    "location": location,
                }
            )

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

        # Alerts (T-24h, T-1h, T-10m)
        for label, delta in ALERT_OFFSETS:
            when = ev["start"] - delta
            if when > now:
                alerts.append(
                    {
                        "when": when,
                        "label": label,
                        "event": ev,
                        "event_id": ev_id,
                    }
                )

        # Reaction at T+10m (for all supported events)
        react_when = ev["start"] + timedelta(minutes=REACTION_WINDOW_MIN)
        if react_when > now:
            reactions.append(
                {
                    "when": react_when,
                    "event": ev,
                    "event_id": ev_id,
                }
            )

    alerts.sort(key=lambda a: a["when"])
    reactions.sort(key=lambda a: a["when"])

    return alerts, reactions


# -------------------------------------------------------------
# Refresh Calendar (HTML mode, no ICS)
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

    # unique (title + start)
    uniq = {(e["title"], e["start"]): e for e in events}
    events = sorted(uniq.values(), key=lambda e: e["start"])

    STATE["events"] = events
    alerts, reactions = _build_queues(events)
    STATE["alert_queue"] = alerts
    STATE["reaction_queue"] = reactions
    STATE["last_refresh"] = datetime.now(timezone.utc)


# -------------------------------------------------------------
# Reaction Logic
# -------------------------------------------------------------

def _capture_pre_event_prices(ev_id: str):
    """
    Capture BTC/ETH prices 10 min BEFORE the event to compare later.
    """
    try:
        btc = get_ticker(BTC_SYM)
        eth = get_ticker(ETH_SYM)
        if btc is None or eth is None:
            return
        STATE["pre_prices"][ev_id] = {"btc": float(btc), "eth": float(eth)}
    except Exception:
        return


def _reaction_for_event(ev, ev_id: str):
    ref = STATE["pre_prices"].get(ev_id)
    if not ref:
        return

    try:
        btc_now = float(get_ticker(BTC_SYM))
        eth_now = float(get_ticker(ETH_SYM))
    except Exception:
        return

    btc_before = ref["btc"]
    eth_before = ref["eth"]

    def pct(now, before):
        return (now - before) / before * 100 if before else 0

    btc_pc = pct(btc_now, btc_before)
    eth_pc = pct(eth_now, eth_before)

    def tag(pc):
        if pc >= REACTION_THRESH_PC:
            return "🟢 Bullish"
        if pc <= -REACTION_THRESH_PC:
            return "🔴 Bearish"
        return "🔵 Neutral"

    msg = (
        f"🏦 [FedWatch] Market Reaction — {ev['title']}\n"
        f"🕒 {_fmt_brussels(ev['start'])}\n\n"
        f"BTC: {btc_pc:+.2f}% {tag(btc_pc)}\n"
        f"ETH: {eth_pc:+.2f}% {tag(eth_pc)}"
    )
    send_text(msg)


# -------------------------------------------------------------
# Scheduler Loop
# -------------------------------------------------------------

def schedule_loop():
    refresh_calendar()
    last_refresh = time.time()

    while True:
        now = datetime.now(timezone.utc)

        # Periodic refresh every 30 minutes
        if time.time() - last_refresh > 1800:
            refresh_calendar()
            last_refresh = time.time()

        # Alerts
        if STATE["alert_queue"]:
            nxt = STATE["alert_queue"][0]
            if now >= nxt["when"]:
                ev = nxt["event"]
                location = ev.get("location", "Federal Reserve")
                send_text(
                    f"🏦 [FedWatch] Alert — {nxt['label']}\n"
                    f"🗓️ {ev['title']}\n"
                    f"🕒 {_fmt_brussels(ev['start'])}\n"
                    f"📍 {location}"
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
        send_text("🏦 [FedWatch] No upcoming FOMC/Fed events found.")
        return

    ev = up[0]
    delta = ev["start"] - now
    hrs, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    location = ev.get("location", "Federal Reserve")

    send_text(
        f"🏦 [FedWatch] Next Event\n"
        f"🗓️ {ev['title']}\n"
        f"🕒 {_fmt_brussels(ev['start'])} (in {hrs}h {mins}m)\n"
        f"📍 {location}"
    )


def _diag_summary(ev):
    return f"{_fmt_brussels(ev['start'])} • {ev['title']}"


def show_diag(n: int = 5):
    if not STATE["events"]:
        refresh_calendar()

    now = datetime.now(timezone.utc)
    upcoming = [e for e in STATE["events"] if e["start"] > now][:n]

    lines = [
        "🏦 [FedWatch] Diagnostics",
        "Source: FOMC HTML Calendar (extended for minutes/testimony when present)",
        f"Status: {'OK ✅' if STATE['events'] else 'NO EVENTS ⚠️'}",
        "",
    ]

    if not upcoming:
        lines.append("No upcoming events.")
    else:
        lines.append("Next events:")
        for ev in upcoming:
            lines.append(f"• {_diag_summary(ev)}")

    send_text("\n".join(lines))
