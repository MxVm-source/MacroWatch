import os, time, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.utils import send_text
from bot.datafeed_bitget import get_ticker

FED_ICS_URL = os.getenv("FED_ICS_URL", "").strip()

ALERT_OFFSETS = [
    ("T-24h", timedelta(hours=24)),
    ("T-1h", timedelta(hours=1)),
    ("T-10m", timedelta(minutes=10)),
]

FED_EVENT_KEYWORDS = os.getenv(
    "FED_EVENT_KEYWORDS",
    "FOMC,Press Conference,Speech,Remarks,Testimony,Minutes,Policy Statement"
).split(",")
FED_EVENT_KEYWORDS = [k.strip().lower() for k in FED_EVENT_KEYWORDS if k.strip()]

REACTION_WINDOW_MIN = int(os.getenv("FED_REACT_WINDOW_MIN", "10"))
REACTION_THRESH_PC  = float(os.getenv("FED_REACT_THRESHOLD_PC", "0.5"))

BTC_SYM = os.getenv("FED_REACT_BTC_SYMBOL", "BTCUSDT_UMCBL")
ETH_SYM = os.getenv("FED_REACT_ETH_SYMBOL", "ETHUSDT_UMCBL")

BRUSSELS_TZ = ZoneInfo("Europe/Brussels")

STATE = {
    "events": [],
    "alert_queue": [],
    "reaction_queue": [],
    "warned": False,
    "source_ok": False,
    "last_refresh": None,
    "pre_prices": {},
}


def _fmt_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_brussels(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(BRUSSELS_TZ)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _event_id(ev) -> str:
    return f"{ev['title']}|{_fmt_utc(ev['start'])}"


def _parse_dt(val: str) -> datetime:
    val = val.strip()
    if val.endswith("Z"):
        return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(val, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def _fetch_ics() -> str:
    if not FED_ICS_URL:
        return ""
    try:
        r = requests.get(FED_ICS_URL, timeout=10)
        if r.ok and "BEGIN:VCALENDAR" in r.text:
            return r.text
    except Exception as e:
        print("FEDWATCH: exception fetching ICS:", e)
    return ""


def _parse_ics(text: str):
    events = []
    if not text:
        return events

    lines = [ln.strip() for ln in text.splitlines()]
    unfolded = []
    for i, ln in enumerate(lines):
        if i > 0 and (ln.startswith(" ") or ln.startswith("\t")):
            unfolded[-1] += ln.strip()
        else:
            unfolded.append(ln)
    lines = unfolded

    cur = {}
    in_ev = False
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
            in_ev = True
        elif ln == "END:VEVENT":
            if cur.get("SUMMARY") and cur.get("DTSTART"):
                try:
                    start = _parse_dt(cur["DTSTART"])
                    title = cur["SUMMARY"].strip()
                    loc = cur.get("LOCATION", "").strip()
                    events.append({"title": title, "start": start, "location": loc})
                except Exception:
                    pass
            cur = {}
            in_ev = False
        elif in_ev:
            if ln.startswith("SUMMARY:"):
                cur["SUMMARY"] = ln.split(":", 1)[1]
            elif ln.startswith("DTSTART"):
                parts = ln.split(":", 1)
                if len(parts) == 2:
                    cur["DTSTART"] = parts[1]
            elif ln.startswith("LOCATION:"):
                cur["LOCATION"] = ln.split(":", 1)[1]
    return events


def _build_queues(events):
    alerts = []
    reactions = []
    now = datetime.now(timezone.utc)
    for ev in events:
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
    return alerts, reactions


def refresh_calendar():
    ics = _fetch_ics()
    if not ics:
        STATE["source_ok"] = False
        if not STATE["warned"]:
            send_text(
                "🏦 [FedWatch] Calendar source unavailable. "
                "Set FED_ICS_URL to a valid Federal Reserve ICS feed "
                "(e.g. https://www.federalreserve.gov/feeds/calendar.ics)."
            )
            STATE["warned"] = True
        STATE["events"] = []
        STATE["alert_queue"] = []
        STATE["reaction_queue"] = []
        return

    STATE["source_ok"] = True
    STATE["warned"] = False
    evs = _parse_ics(ics)

    if FED_EVENT_KEYWORDS:
        filtered = []
        for e in evs:
            title_l = e["title"].lower()
            if any(k in title_l for k in FED_EVENT_KEYWORDS):
                filtered.append(e)
        evs = filtered

    uniq = {}
    for e in evs:
        key = (e["title"], e["start"])
        uniq[key] = e
    evs = list(uniq.values())
    evs.sort(key=lambda e: e["start"])
    STATE["events"] = evs

    alerts, reactions = _build_queues(evs)
    STATE["alert_queue"] = alerts
    STATE["reaction_queue"] = reactions
    STATE["last_refresh"] = datetime.now(timezone.utc)


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
    except Exception as e:
        print("FEDWATCH: reaction price exception:", e)
        btc_now = None
        eth_now = None

    if not ref or btc_now is None or eth_now is None:
        return

    def pct_change(now, before):
        if before == 0 or before is None or now is None:
            return 0.0
        return (now - before) / before * 100.0

    btc_pc = pct_change(btc_now, ref["btc"])
    eth_pc = pct_change(eth_now, ref["eth"])

    def classify(pc):
        if pc >= REACTION_THRESH_PC:
            return "bullish", "🟢"
        elif pc <= -REACTION_THRESH_PC:
            return "bearish", "🔴"
        else:
            return "neutral", "🔵"

    btc_sent, btc_emo = classify(btc_pc)
    eth_sent, eth_emo = classify(eth_pc)

    if btc_sent == "bullish" and eth_sent == "bullish":
        flag = "🟢 Bullish reaction for crypto (initial move)"
    elif btc_sent == "bearish" and eth_sent == "bearish":
        flag = "🔴 Bearish reaction for crypto (initial move)"
    else:
        flag = "🔵 Mixed / neutral reaction (initial move)"

    msg = (
        f"🏦 [FedWatch] Market Reaction — {ev['title']}\n"
        f"🕒 Event Time: {_fmt_brussels(ev['start'])} (Brussels)\n"
        f"⏱️ Reaction Window: first {REACTION_WINDOW_MIN} minutes\n\n"
        f"BTC: {btc_pc:+.2f}% {btc_emo}\n"
        f"ETH: {eth_pc:+.2f}% {eth_emo}\n\n"
        f"📊 Flag: {flag}"
    )
    send_text(msg)


def schedule_loop():
    refresh_calendar()
    last_refresh_wall = time.time()

    while True:
        now = datetime.now(timezone.utc)

        if time.time() - last_refresh_wall > 1800:
            refresh_calendar()
            last_refresh_wall = time.time()

        if STATE["alert_queue"]:
            nxt = STATE["alert_queue"][0]
            if now >= nxt["when"]:
                ev = nxt["event"]
                ev_id = nxt["event_id"]
                label = nxt["label"]
                send_text(
                    f"🏦 [FedWatch] Alert — {label}\n"
                    f"🗓️ {ev['title']}\n"
                    f"🕒 {_fmt_brussels(ev['start'])} (Brussels)\n"
                    f"📍 {ev.get('location','')}"
                )
                if label == "T-10m":
                    _capture_pre_event_prices(ev_id)
                STATE["alert_queue"].pop(0)

        if STATE["reaction_queue"]:
            nxt_r = STATE["reaction_queue"][0]
            if now >= nxt_r["when"]:
                _reaction_for_event(nxt_r["event"], nxt_r["event_id"])
                STATE["reaction_queue"].pop(0)

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
    hrs, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    send_text(
        f"🏦 [FedWatch] Upcoming Event\n"
        f"🗓️ {ev['title']}\n"
        f"🕒 {_fmt_brussels(ev['start'])} (Brussels) (in {hrs}h {mins}m)\n"
        f"📍 {ev.get('location','')}"
    )


def _diag_summary(ev):
    loc = ev.get("location", "").strip()
    loc_part = f" — {loc}" if loc else ""
    return f"{_fmt_brussels(ev['start'])} (Brussels) • {ev['title']}{loc_part}"


def show_diag(n: int = 5):
    if not STATE["events"]:
        refresh_calendar()

    src = FED_ICS_URL or "(not set)"
    status = "OK ✅" if STATE.get("source_ok") and STATE["events"] else "NO EVENTS ⚠️"
    last = STATE.get("last_refresh")
    if last is None:
        last_str = "unknown"
    else:
        last_str = last.astimezone(BRUSSELS_TZ).strftime("%Y-%m-%d %H:%M %Z")

    now = datetime.now(timezone.utc)
    upcoming = [e for e in STATE["events"] if e["start"] > now][:n]

    lines = [
        "🏦 [FedWatch] Diagnostics",
        f"Source: {src}",
        f"Status: {status}",
        f"Last refresh: {last_str}",
    ]

    if not upcoming:
        lines.append("No upcoming events found.")
    else:
        lines.append("")
        lines.append("Next events:")
        for ev in upcoming:
            lines.append(f"• {_diag_summary(ev)}")

    send_text("\n".join(lines))
