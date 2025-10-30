import os, time, requests
from datetime import datetime, timedelta, timezone
from bot.utils import send_text

FED_ICS_URL = os.getenv("FED_ICS_URL", "").strip()
ALERT_OFFSETS = [("T-24h", timedelta(hours=24)), ("T-1h", timedelta(hours=1)), ("T-10m", timedelta(minutes=10))]

STATE = {
    "events": [],          # list of dicts {title,start,location}
    "alert_queue": [],     # list of dicts {when,label,event}
    "warned": False        # prevent repeated warning spam
}

def _parse_dt(val: str) -> datetime:
    # Handles DTSTART:20251101T150000Z or DTSTART:20251101T150000
    val = val.strip()
    if val.endswith("Z"):
        return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    # assume UTC if no timezone
    return datetime.strptime(val, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)

def _fetch_ics():
    if not FED_ICS_URL:
        return ""
    try:
        r = requests.get(FED_ICS_URL, timeout=10)
        if r.ok and "BEGIN:VCALENDAR" in r.text:
            return r.text
    except Exception:
        pass
    return ""

def _parse_ics(text: str):
    events = []
    if not text:
        return events
    lines = [ln.strip() for ln in text.splitlines()]
    cur = {}
    in_ev = False
    # Handle folded lines (RFC 5545) — join lines that start with space
    unfolded = []
    for i, ln in enumerate(lines):
        if i > 0 and (ln.startswith(" ") or ln.startswith("\t")):
            unfolded[-1] += ln.strip()
        else:
            unfolded.append(ln)
    lines = unfolded
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}; in_ev = True
        elif ln == "END:VEVENT":
            if cur.get("SUMMARY") and cur.get("DTSTART"):
                try:
                    start = _parse_dt(cur["DTSTART"])
                    title = cur["SUMMARY"].strip()
                    loc = cur.get("LOCATION", "").strip()
                    events.append({"title": title, "start": start, "location": loc})
                except Exception:
                    pass
            cur = {}; in_ev = False
        elif in_ev:
            if ln.startswith("SUMMARY:"):
                cur["SUMMARY"] = ln.split(":",1)[1]
            elif ln.startswith("DTSTART"):
                # DTSTART;TZID=America/New_York:20251101T150000 or DTSTART:20251101T150000Z
                parts = ln.split(":",1)
                if len(parts)==2:
                    cur["DTSTART"] = parts[1]
            elif ln.startswith("LOCATION:"):
                cur["LOCATION"] = ln.split(":",1)[1]
    return events

def _fmt(dt: datetime) -> str:
    # always show UTC for consistency
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _build_alerts(events):
    alerts = []
    now = datetime.now(timezone.utc)
    for ev in events:
        if ev["start"] <= now:
            continue  # skip past events
        for label, delta in ALERT_OFFSETS:
            when = ev["start"] - delta
            if when > now:
                alerts.append({"when": when, "label": label, "event": ev})
    alerts.sort(key=lambda a: a["when"])
    return alerts

def refresh_calendar():
    # Fetch & parse ICS, update STATE
    ics = _fetch_ics()
    if not ics:
        if not STATE["warned"]:
            send_text("🏦 [FedWatch] Calendar source unavailable. Set FED_ICS_URL to the Federal Reserve ICS feed to enable accurate alerts.")
            STATE["warned"] = True
        STATE["events"] = []
        STATE["alert_queue"] = []
        return
    evs = _parse_ics(ics)
    # Keep only unique by (title,start)
    uniq = {}
    for e in evs:
        key = (e["title"], e["start"])
        uniq[key] = e
    evs = list(uniq.values())
    # Sort by start
    evs.sort(key=lambda e: e["start"])
    STATE["events"] = evs
    STATE["alert_queue"] = _build_alerts(evs)

def schedule_loop():
    # initial load
    refresh_calendar()
    last_refresh = time.time()
    while True:
        now = datetime.now(timezone.utc)
        # periodic refresh every 30 minutes
        if time.time() - last_refresh > 1800:
            refresh_calendar()
            last_refresh = time.time()

        # pop due alerts
        if STATE["alert_queue"]:
            nxt = STATE["alert_queue"][0]
            if now >= nxt["when"]:
                ev = nxt["event"]
                send_text(f"🏦 [FedWatch] Alert — {nxt['label']}\n🗓️ {ev['title']}\n🕒 {_fmt(ev['start'])}\n📍 {ev.get('location','')}")
                STATE["alert_queue"].pop(0)
        time.sleep(5)

def show_next_event():
    if not STATE["events"]:
        refresh_calendar()
    now = datetime.now(timezone.utc)
    upcoming = [e for e in STATE["events"] if e["start"] > now]
    if not upcoming:
        send_text("🏦 [FedWatch] No upcoming events found from ICS.")
        return
    ev = upcoming[0]
    delta = ev["start"] - now
    hrs, rem = divmod(int(delta.total_seconds()), 3600); mins = rem // 60
    send_text(f"🏦 [FedWatch] Upcoming Event\n🗓️ {ev['title']}\n🕒 {_fmt(ev['start'])} (in {hrs}h {mins}m)\n📍 {ev.get('location','')}")
