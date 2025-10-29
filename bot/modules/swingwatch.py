import os, math, tempfile, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from bot.utils import send_text, send_photo
from bot.datafeed_bitget import get_ticker, get_candles
from bot.modules import liquidation
SYMS=os.getenv('BITGET_SYMBOLS','BTCUSDT_UMCBL,ETHUSDT_UMCBL').split(','); DISPLAY={'BTCUSDT_UMCBL':'BTCUSDT','ETHUSDT_UMCBL':'ETHUSDT'}
GRAN=int(os.getenv('BITGET_GRANULARITY_SEC','14400'))
def _pivots(highs,lows):
    piv_hi=[]; piv_lo=[]
    for i in range(1,len(highs)-1):
        if highs[i]>highs[i-1] and highs[i]>highs[i+1]: piv_hi.append((i,highs[i]))
        if lows[i]<lows[i-1] and lows[i]<lows[i+1]: piv_lo.append((i,lows[i]))
    return piv_hi,piv_lo
def _nearest_levels(price,piv_hi,piv_lo):
    res=min([v for _,v in piv_hi if v>=price] or [math.inf], key=lambda x: x-price if x!=math.inf else 1e9)
    sup=max([v for _,v in piv_lo if v<=price] or [-math.inf], key=lambda x: price-x if x!=-math.inf else 1e9)
    return (None if res==math.inf else res),(None if sup==-math.inf else sup)
def _render_chart(sym, closes, price, res, sup, entry_low, entry_high, sl, bias, liq_levels):
    import numpy as _np
    x=_np.arange(len(closes)); plt.close('all'); fig=plt.figure(figsize=(9,6),dpi=120); ax=fig.add_subplot(111)
    fig.patch.set_facecolor('#0b0f14'); ax.set_facecolor('#0b0f14'); ax.tick_params(colors='#9aa4ad')
    for s in ax.spines.values(): s.set_color('#22303a')
    ax.grid(True,color='#1a232c',linewidth=0.5,alpha=0.6)
    ax.plot(x,closes,linewidth=2); ax.axhline(price,linestyle='-',linewidth=1.2)
    if res: ax.axhline(res,linestyle='--',linewidth=1.5)
    if sup: ax.axhline(sup,linestyle='--',linewidth=1.5)
    ax.axhspan(entry_low,entry_high,alpha=0.20,linewidth=0); ax.axhline(sl,linestyle='--',linewidth=1.8)
    for lvl in liq_levels: ax.axhline(lvl['price'],linestyle=':',linewidth=1.6)
    ax.set_title(f"SwingWatch | {DISPLAY.get(sym,sym)} | Bitget 4H + Binance Liquidity ({bias.upper()})",color='white',fontsize=11)
    ax.set_xlim(x[0],x[-1]); import tempfile as _tf; tmp=_tf.NamedTemporaryFile(delete=False,suffix='.png'); fig.savefig(tmp.name,bbox_inches='tight'); plt.close(fig); return tmp.name
def run_scan_post():
    th=int(os.getenv('LIQ_THRESHOLD_USD','150000000')); prox=float(os.getenv('LIQ_PROXIMITY_PCT','0.6'))
    for sym in SYMS:
        base=DISPLAY.get(sym,sym); candles=get_candles(sym,granularity_sec=GRAN,limit=180)
        if len(candles)<50: send_text(f'🎯 [SwingWatch] Not enough candles for {base}'); continue
        ts,o,h,l,c=zip(*candles); price=get_ticker(sym) or c[-1]
        if abs(price-c[-1])/max(price,1e-9)>0.02: price=c[-1]
        piv_hi,piv_lo=_pivots(h,l); res,sup=_nearest_levels(price,piv_hi,piv_lo)
        clusters=liquidation.get_clusters(base,price)
        confl=[cl for cl in clusters if (res and liquidation.is_confluent(cl['price'],res)) or (sup and liquidation.is_confluent(cl['price'],sup))]
        last_green=c[-1]>o[-1]
        if confl:
            best=sorted(confl,key=lambda x:abs(x['price']-price))[0]; near_res = res and abs(best['price']-res)<=abs(best['price']-(sup or best['price']+1e9))
            if near_res and not last_green:
                bias='bearish'; level=res; emoji='🔻'; entry_low=level*(1-0.004); entry_high=level*(1-0.001); sl=level*1.01; confl_txt=f"🔥 Confluence: Liquidity ${best['usd']:,} @ {best['price']:,.0f} near RES"
            else:
                bias='bullish'; level=sup or best['price']; emoji='🟢'; entry_low=level*(1+0.001); entry_high=level*(1+0.004); sl=level*0.99; confl_txt=f"🔥 Confluence: Liquidity ${best['usd']:,} @ {best['price']:,.0f} near SUP"
            plot_liq=confl
        else:
            if res and abs((res-price)/price)<0.008 and not last_green:
                bias='bearish'; level=res; emoji='🔻'; entry_low=level*(1-0.004); entry_high=level*(1-0.001); sl=level*1.01
            else:
                bias='bullish'; level=sup or c[-20]; emoji='🟢'; entry_low=level*(1+0.001); entry_high=level*(1+0.004); sl=level*0.99
            confl_txt='No liquidity confluence ≥ threshold'; plot_liq=clusters[:2]
        cap=(f"🎯 [SwingWatch] {base} — 4H Bitget + Binance Liquidity {emoji} {bias.upper()}\n"
             f"⚡ Price: {price:,.0f}\nResistance: {res:,.0f} | Support: {sup:,.0f}\n"
             f"🎯 Entry: {entry_low:,.0f} – {entry_high:,.0f} | ⛔ SL: {sl:,.0f} (±1%)\n{confl_txt} (≥ ${th/1_000_000:.0f}M | ±{prox:.2f}%)")
        img=_render_chart(sym,c,price,res,sup,entry_low,entry_high,sl,bias,plot_liq); send_photo(cap,img)
    send_text('✅ SwingWatch Scan Complete\n🕒 4H Confluence')
def show_latest():
    send_text('🎯 [SwingWatch] Live mode ready. Use /next now or wait for the 4H schedule.')