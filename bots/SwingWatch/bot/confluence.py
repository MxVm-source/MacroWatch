import os, random, tempfile
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Build confluences from mock zones + tlines
def find_confluences(zones, tlines):
    czs = []
    for z in zones:
        sym = z["symbol"]
        zone_center = z["zone_center"]
        zone_width_pct = round(random.uniform(0.4, 0.7), 2)
        # mock current price near zone with slight offset for bias
        bias_bear = random.choice([True, False])
        direction = "bearish" if bias_bear else "bullish"
        emoji = "🔻" if bias_bear else "🟢"
        # distance sign: positive means overhead
        dist_pct = round(random.uniform(0.5, 5.5) * (1 if bias_bear else -1), 2)
        # entry band inside zone
        span = zone_center * zone_width_pct/100.0
        if bias_bear:
            entry_low = zone_center - span*0.6
            entry_high = zone_center - span*0.1
            sl = (zone_center + span) * 1.01
        else:
            entry_low = zone_center + span*0.1
            entry_high = zone_center + span*0.6
            sl = (zone_center - span) * 0.99
        tl_type = "Descending Resistance" if bias_bear else "Ascending Support"
        touches = random.randint(3,5)
        czs.append({
            "symbol": sym,
            "tl_tf": "4H",
            "tl_type": tl_type,
            "touches": touches,
            "zone_center": round(zone_center,2),
            "zone_width_pct": zone_width_pct,
            "total_usd": z["total_usd"],
            "binance_usd": z["binance_usd"],
            "bybit_usd": z["bybit_usd"],
            "entry_low": round(entry_low,2),
            "entry_high": round(entry_high,2),
            "stop_loss": round(sl,2),
            "direction": direction,
            "bias": direction.upper(),
            "emoji": emoji,
            "distance_pct": dist_pct
        })
    return czs

def pick_best_zone(zlist):
    if not zlist:
        return None
    return sorted(zlist, key=lambda z: (abs(z.get('distance_pct', 999)), -z.get('total_usd', 0)))[0]

def check_price_hits(on_hit, on_retest):
    # Mock mode: skip; real-time feed to be wired later
    return

def render_chart(cz):
    if os.getenv("POST_IMAGE_ON_SCAN","true").lower() not in ("1","true","yes","on"):
        return None
    # Simple dark TV-style mock chart
    sym = cz["symbol"]
    center = cz["zone_center"]
    span = center * cz.get("zone_width_pct",0)/100.0
    # mock time series
    np.random.seed(int(datetime.utcnow().timestamp()) % 100000)
    base = center * (1 - (cz["distance_pct"]/100.0))
    series = base * (1 + 0.002*np.cumsum(np.random.randn(60)))
    x = np.arange(len(series))

    plt.close('all')
    fig = plt.figure(figsize=(9,6), dpi=120)
    ax = fig.add_subplot(111)
    # dark theme
    fig.patch.set_facecolor('#0b0f14')
    ax.set_facecolor('#0b0f14')
    ax.tick_params(colors='#9aa4ad')
    for spine in ax.spines.values():
        spine.set_color('#22303a')
    ax.grid(True, color='#1a232c', linewidth=0.5, alpha=0.6)

    # price path
    ax.plot(x, series, linewidth=2)

    # liquidity zone
    ax.axhspan(center - span, center + span, alpha=0.15, linewidth=0, color='#8a2be2')

    # entry zone
    ax.axhspan(cz["entry_low"], cz["entry_high"], alpha=0.20, linewidth=0, color='#00ff00')
    # stop loss
    ax.axhline(cz["stop_loss"], linestyle='--', linewidth=1.8, color='#ff4d4d')

    # trendlines (mock: diagonal lines across chart)
    # Resistance (red-like)
    ax.plot([x[0], x[-1]], [center+span*1.5, center+span*0.5], linewidth=1.8)
    # Support (green-like)
    ax.plot([x[0], x[-1]], [center-span*1.5, center-span*0.5], linewidth=1.8)

    ax.set_title(f"SwingWatch | {sym} | Mock Dynamic Drift + Trendlines", color='white', fontsize=11)
    ax.set_xlim(x[0], x[-1])

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    fig.savefig(tmp.name, bbox_inches='tight')
    plt.close(fig)
    return tmp.name
