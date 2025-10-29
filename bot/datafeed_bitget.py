import requests
API='https://api.bitget.com'
def get_ticker(symbol='BTCUSDT_UMCBL'):
    r=requests.get(f'{API}/api/v2/market/tickers',params={'symbol':symbol},timeout=8)
    if r.ok:
        j=r.json()
        if j.get('data'):
            it=j['data'][0]; val=it.get('lastPr') or it.get('last')
            try: return float(val)
            except: pass
    r=requests.get(f'{API}/api/mix/v1/market/ticker',params={'symbol':symbol,'productType':'umcbl'},timeout=8)
    if r.ok:
        j=r.json()
        if j.get('data') and j['data'].get('last'):
            try: return float(j['data']['last'])
            except: return None
    return None
def get_candles(symbol='BTCUSDT_UMCBL',granularity_sec=14400,limit=200):
    r=requests.get(f'{API}/api/v2/market/candles',params={'symbol':symbol,'granularity':str(granularity_sec)},timeout=10)
    if r.ok:
        rows=r.json().get('data',[]); out=[]
        for row in rows[:limit]:
            try:
                ts=int(row[0])//1000; o,h,l,c=map(float,row[1:5]); out.append((ts,o,h,l,c))
            except: continue
        out.reverse(); return out
    r=requests.get(f'{API}/api/mix/v1/market/candles',params={'symbol':symbol,'granularity':granularity_sec,'limit':limit},timeout=10)
    if r.ok:
        rows=r.json().get('data',[]); out=[]
        for row in rows[:limit]:
            try:
                ts=int(row[0])//1000; o,h,l,c=map(float,row[1:5]); out.append((ts,o,h,l,c))
            except: continue
        out.reverse(); return out
    return []