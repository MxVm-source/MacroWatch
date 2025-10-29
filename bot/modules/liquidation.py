import os, math, requests
from collections import defaultdict
THRESHOLD=int(os.getenv('LIQ_THRESHOLD_USD','150000000')); PROX_PCT=float(os.getenv('LIQ_PROXIMITY_PCT','0.6'))
BIN_DEPTH_LIMIT=int(os.getenv('BIN_DEPTH_LIMIT','1000')); BIN_BUCKET_BTC=float(os.getenv('BIN_BUCKET_BTC','100')); BIN_BUCKET_ETH=float(os.getenv('BIN_BUCKET_ETH','10'))
BIN_SYMBOLS={'BTCUSDT':'BTCUSDT','ETHUSDT':'ETHUSDT'}; FAPI='https://fapi.binance.com'
def _bucket(price,base): w=BIN_BUCKET_BTC if base=='BTCUSDT' else BIN_BUCKET_ETH; return round(math.floor(price/w)*w,2)
def _depth(symbol):
    try:
        r=requests.get(f'{FAPI}/fapi/v1/depth',params={'symbol':symbol,'limit':BIN_DEPTH_LIMIT},timeout=6)
        if r.ok: j=r.json(); return j.get('bids',[]), j.get('asks',[])
    except Exception: pass
    return [],[]
def _force_orders(symbol,limit=200):
    try:
        r=requests.get(f'{FAPI}/fapi/v1/allForceOrders',params={'symbol':symbol,'limit':limit},timeout=6)
        if r.ok: return r.json() or []
    except Exception: pass
    return []
def get_clusters(display_symbol, spot_price):
    base=BIN_SYMBOLS.get(display_symbol,display_symbol); bids,asks=_depth(base); buckets=defaultdict(float)
    for px,qty in bids:
        try: p=float(px); q=float(qty); buckets[_bucket(p,base)] += p*q
        except: pass
    for px,qty in asks:
        try: p=float(px); q=float(qty); buckets[_bucket(p,base)] += p*q
        except: pass
    for it in _force_orders(base,limit=200):
        try:
            p=float(it.get('price') or 0.0); q=float(it.get('origQty') or 0.0)
            if p>0 and q>0: buckets[_bucket(p,base)] += p*q
        except: pass
    clusters=[{'price':bp,'usd':int(val)} for bp,val in buckets.items() if float(val)>=THRESHOLD]
    clusters.sort(key=lambda x:(abs(x['price']-spot_price), -x['usd'])); return clusters
def is_confluent(spot_price, level_price):
    if not level_price: return False
    pct=abs(level_price-spot_price)/max(1e-9,float(spot_price))*100.0; return pct<=PROX_PCT