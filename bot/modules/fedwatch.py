import os, time
from datetime import datetime, timedelta
from bot.utils import send_text

STATE = {"events": []}

def seed_mock_events():
    now = datetime.utcnow()
    STATE["events"] = [
        {"title":"FOMC Press Conference", "start": now + timedelta(minutes=90), "location":"Washington, D.C."},
        {"title":"Powell Speech", "start": now + timedelta(hours=6), "location":"Jackson Hole (virtual)"},
    ]

def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")

def schedule_loop():
    # mock alerts at T-24h, T-1h, T-10m (future only)
    if not STATE["events"]:
        seed_mock_events()
    alerts = []
    now = datetime.utcnow()
    for ev in STATE["events"]:
        for label, delta in (("T-24h", timedelta(hours=24)), ("T-1h", timedelta(hours=1)), ("T-10m", timedelta(minutes=10))):
            when = ev["start"] - delta
            if when > now:
                alerts.append({"when": when, "label": label, "event": ev})
    alerts.sort(key=lambda a: a["when"])
    idx = 0
    while True:
        now = datetime.utcnow()
        if idx < len(alerts) and now >= alerts[idx]["when"]:
            ev = alerts[idx]["event"]
            label = alerts[idx]["label"]
            send_text(f"🏦 [FedWatch] Alert — {label}\n🗓️ {ev['title']}\n🕒 {_fmt(ev['start'])}\n📍 {ev['location']}")
            idx += 1
        time.sleep(5)

def show_next_event():
    if not STATE["events"]:
        seed_mock_events()
    now = datetime.utcnow()
    next_ev = None
    for ev in sorted(STATE["events"], key=lambda e: e["start"]):
        if ev["start"] > now:
            next_ev = ev
            break
    if not next_ev:
        send_text("🏦 [FedWatch] No upcoming events.")
        return
    delta = next_ev["start"] - now
    hrs, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    send_text(f"🏦 [FedWatch] Upcoming Event\n🗓️ {next_ev['title']}\n🕒 {_fmt(next_ev['start'])} (in {hrs}h {mins}m)\n📍 {next_ev['location']}")
