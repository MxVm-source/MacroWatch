import os, requests
TOKEN=os.getenv('TELEGRAM_TOKEN'); CHAT_ID=os.getenv('CHAT_ID'); BASE=f'https://api.telegram.org/bot{TOKEN}' if TOKEN else None
def send_text(t):
    if not BASE or not CHAT_ID: return False
    try:
        return requests.post(f'{BASE}/sendMessage',data={'chat_id':CHAT_ID,'text':t,'parse_mode':'HTML','disable_web_page_preview':True},timeout=15).ok
    except Exception: return False
def send_photo(caption, path):
    if not BASE or not CHAT_ID: return False
    try:
        with open(path,'rb') as f:
            return requests.post(f'{BASE}/sendPhoto',data={'chat_id':CHAT_ID,'caption':caption,'parse_mode':'HTML'},files={'photo':f},timeout=30).ok
    except Exception: return False
def get_updates(offset=None,timeout=20):
    if not BASE: return {'ok':False,'result':[]}
    try:
        p={'timeout':timeout}; 
        if offset is not None: p['offset']=offset
        return requests.get(f'{BASE}/getUpdates',params=p,timeout=timeout+5).json()
    except Exception: return {'ok':False,'result':[]}