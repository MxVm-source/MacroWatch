import random
from datetime import datetime, timedelta
from bot.utils import send_text
HEADLINES=[('Fiscal','Trump signals corporate tax adjustments to spur growth'),('Tariff','Tariff stance hardens amid talks with China leadership'),('Regulation','Banking deregulation on the table to boost credit'),('Energy','Oil & gas permits to expand; renewables incentives reviewed'),('Crypto','Bitcoin custody rules for banks under review'),('Geopolitics','Sanctions shift could impact commodity flows')]
STATE={'history':[]}; IMPACT_MIN=0.7; DEDUP_HOURS=6
def _impact_and_sent(title):
    t=title.lower(); bull=sum(w in t for w in ['growth','lower','boost','expand','incentives','credit','rebound','deregulation']); bear=sum(w in t for w in ['hardens','sanctions','tariff','restrict','cut','tighten','shutdown','war'])
    base=0.6+0.1*(bull+bear); impact=max(0.0,min(0.95,base)); sent='bullish' if bull>bear else 'bearish' if bear>bull else 'neutral'; emo={'bullish':'🟢📈','bearish':'🔴📉','neutral':'🔵⚖️'}[sent]; return impact,sent,emo
def _recent_titles():
    now=datetime.utcnow(); cutoff=now-timedelta(hours=DEDUP_HOURS); return set(h['title'] for h in STATE['history'] if datetime.fromisoformat(h['t'])>=cutoff)
def post_mock(force=False):
    recent=_recent_titles(); tried=0; picked=None
    while tried<10:
        tags,title=random.choice(HEADLINES); tried+=1; impact,sent,emo=_impact_and_sent(title)
        if force or (impact>=IMPACT_MIN and title not in recent): picked=(tags,title,impact,sent,emo); break
    if not picked: return False
    tags,title,impact,sent,emo=picked; msg=(f"🍊 [TrumpWatch] ⚠️ Market Impact: HIGH ({impact:.2f}) | Sentiment: {emo} {sent.title()}\n📎 Tags: {tags}\n🗞️ {title}"); send_text(msg)
    STATE['history'].append({'t':datetime.utcnow().isoformat(timespec='minutes'),'tags':tags,'title':title,'impact':impact,'sent':sent})
    if len(STATE['history'])>50: STATE['history']=STATE['history'][-50:]; return True
def show_recent(n=5):
    hist=STATE['history'][-n:]
    if not hist: send_text('🍊 [TrumpWatch] No recent items yet.'); return
    lines=['🗂️ Recent TrumpWatch Items:']+[f"- {it['t']} • {it['tags']} • {it['title']} (impact {it['impact']:.2f}, {it['sent']})" for it in hist]; send_text('\n'.join(lines))