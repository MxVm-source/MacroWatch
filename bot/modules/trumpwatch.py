import os, time, random
from datetime import datetime, timedelta
from bot.utils import send_text

HEADLINES = [
    ("Fiscal", "Trump signals corporate tax adjustments to spur growth"),
    ("Tariff", "Tariff stance hardens amid talks with China leadership"),
    ("Regulation", "Banking deregulation on the table to boost credit"),
    ("Energy", "Oil & gas permits to expand; renewables incentives reviewed"),
    ("Crypto", "Bitcoin custody rules for banks under review"),
    ("Geopolitics", "Sanctions shift could impact commodity flows"),
]

STATE = {
    "history": [],
    "last_keys": {}
}

def _sentiment(title):
    t = title.lower()
    bull = sum(w in t for w in ["growth","lower","boost","expand","incentives","credit","rebound"])
    bear = sum(w in t for w in ["hardens","sanctions","tariff","restrict","cut","tighten"])
    if bull > bear: return "bullish", 0.78
    if bear > bull: return "bearish", 0.78
    return "neutral", 0.55

def _emoji(sent):
    return {"bullish":"🟢📈","bearish":"🔴📉","neutral":"🔵⚖️"}.get(sent,"⚪️")

def post_mock(force=False):
    tags, title = random.choice(HEADLINES)
    sent, score = _sentiment(title)
    impact = round(random.uniform(0.5, 0.9), 2)
    key = f"{tags}|{title.split()[0].lower()}"
    now = datetime.utcnow()
    # cooldown 45 min
    if not force:
        last = STATE["last_keys"].get(key)
        if last and (now - last) < timedelta(minutes=45):
            return False
    msg = (f"🍊 [TrumpWatch] ⚠️ Market Impact: {('LOW','MED','HIGH')[0 if impact<0.55 else 1 if impact<0.75 else 2]} ({impact}) | "
           f"Sentiment: {_emoji(sent)} {sent.title()} ({score})\n"
           f"📎 Tags: {tags}\n"
           f"🗞️ {title}")
    send_text(msg)
    STATE["last_keys"][key] = now
    STATE["history"].append({"t": now.strftime("%Y-%m-%d %H:%M UTC"), "tags":tags, "title":title, "sent":sent, "impact":impact})
    if len(STATE["history"]) > 10:
        STATE["history"].pop(0)
    return True

def show_recent(n=5):
    hist = STATE["history"][-n:]
    if not hist:
        send_text("🍊 [TrumpWatch] No recent items yet.")
        return
    lines = ["🗂️ Recent TrumpWatch Items:"]
    for it in hist:
        lines.append(f"- {it['t']} • {it['tags']} • {it['title']} ({it['sent']}, {it['impact']})")
    send_text("\n".join(lines))
