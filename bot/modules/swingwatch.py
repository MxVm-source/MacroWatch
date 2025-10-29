import os, random, tempfile
from datetime import datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from bot.utils import send_text, send_photo
from bot.modules.liquidation import get_clusters

STATE = {
    "BTCUSDT": {"price": float(os.getenv("SW_BTC_CENTER","113000"))},
    "ETHUSDT": {"price": float(os.getenv("SW_ETH_CENTER","3984"))},
    "last_confluence": {}
}

def liq_ping():
    # try BTCUSDT against current price sample
    sym = "BTCUSDT_UMCBL"
    base = "BTCUSDT"
    from bot.datafeed_bitget import get_ticker
    price = get_ticker(sym) or 0
    clusters = get_clusters(base, price, use_mock=False)
    lines = [f"🔎 Liquidity provider: {os.getenv('LIQ_PROVIDER','mock')}"]
    lines.append(f"Spot ~ {price:,.0f}")
    if not clusters:
        lines.append("No clusters returned (check API URL/key/mapping).")
    else:
        lines.append("Top clusters:")
        for c in clusters[:5]:
            lines.append(f"• {c['price']:,.0f} — ${c['usd']:,}")
    from bot.utils import send_text
    send_text("\n".join(lines))

def _gen_confluence(symbol: str):
    center = STATE[symbol]["price"]
    dmin = float(os.getenv("SWINGWATCH_DRIFT_MIN","3"))
    dmax = float(os.getenv("SWINGWATCH_DRIFT_MAX","8"))
    drift_pct = random.uniform(dmin, dmax) * random.choice([-1,1])
    center = center * (1 + drift_pct/100.0)
    STATE[symbol]["price"] = center

    zone_width_pct = round(random.uniform(0.4, 0.7), 2)
    span = center * zone_width_pct/100.0
    bearish = random.choice([True, False])
    direction = "bearish" if bearish else "bullish"
    emoji = "🔻" if bearish else "🟢"
    dist_pct = round(random.uniform(0.5,5.5) * (1 if bearish else -1), 2)
    if bearish:
        entry_low = center - span*0.6
        entry_high = center - span*0.1
        sl = (center + span) * 1.01
        tl = "Descending Resistance"
    else:
        entry_low = center + span*0.1
        entry_high = center + span*0.6
        sl = (center - span) * 0.99
        tl = "Ascending Support"
    total = random.randint(150_000_000, 600_000_000) if symbol=="BTCUSDT" else random.randint(100_000_000, 400_000_000)
    bin_part = int(total * random.uniform(0.45, 0.65))
    byb_part = total - bin_part
    cz = {
        "symbol": symbol,
        "zone_center": round(center,2),
        "zone_width_pct": zone_width_pct,
        "entry_low": round(entry_low,2),
        "entry_high": round(entry_high,2),
        "stop_loss": round(sl,2),
        "direction": direction,
        "distance_pct": dist_pct,
        "total_usd": total,
        "binance_usd": bin_part,
        "bybit_usd": byb_part,
        "tl": tl,
        "emoji": emoji
    }
    STATE["last_confluence"][symbol] = cz
    return cz

def _render_chart(cz):
    center = cz["zone_center"]
    span = center * cz["zone_width_pct"]/100.0
    np.random.seed(int(datetime.utcnow().timestamp()) % 100000)
    base = center * (1 - (cz["distance_pct"]/100.0))
    series = base * (1 + 0.002*np.cumsum(np.random.randn(60)))
    x = np.arange(len(series))

    plt.close('all')
    fig = plt.figure(figsize=(9,6), dpi=120)
    ax = fig.add_subplot(111)
    # dark style
    fig.patch.set_facecolor('#0b0f14')
    ax.set_facecolor('#0b0f14')
    ax.tick_params(colors='#9aa4ad')
    for spine in ax.spines.values():
        spine.set_color('#22303a')
    ax.grid(True, color='#1a232c', linewidth=0.5, alpha=0.6)

    ax.plot(x, series, linewidth=2)

    # liquidity zone
    ax.axhspan(center - span, center + span, alpha=0.15, linewidth=0, color='#8a2be2')
    # entry zone
    ax.axhspan(cz["entry_low"], cz["entry_high"], alpha=0.2, linewidth=0, color='#00ff00')
    # stop loss
    ax.axhline(cz["stop_loss"], linestyle='--', linewidth=1.8, color='#ff4d4d')
    # trendlines
    ax.plot([x[0], x[-1]], [center+span*1.5, center+span*0.5], linewidth=1.8)  # resistance
    ax.plot([x[0], x[-1]], [center-span*1.5, center-span*0.5], linewidth=1.8)  # support

    ax.set_title(f"SwingWatch | {cz['symbol']} | Mock Dynamic Drift + Trendlines", color='white', fontsize=11)
    ax.set_xlim(x[0], x[-1])

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    fig.savefig(tmp.name, bbox_inches='tight')
    plt.close(fig)
    return tmp.name

def run_scan_post():
    # generate BTC + ETH and post
    for sym in ("BTCUSDT","ETHUSDT"):
        cz = _gen_confluence(sym)
        caption = (
            f"🎯 [SwingWatch] Confluence Reversal Setup {cz['emoji']} {cz['direction'].upper()} — {sym}\n"
            f"Liquidity Zone: {cz['zone_center']:,.0f} ±{cz['zone_width_pct']:.2f}%\n"
            f"Total: ${cz['total_usd']:,} (Bin ${cz['binance_usd']:,} | Byb ${cz['bybit_usd']:,})\n\n"
            f"🎯 Entry: {cz['entry_low']:,.0f} – {cz['entry_high']:,.0f} | ⛔ SL: {cz['stop_loss']:,.0f} (±1%)\n"
            f"TL: {cz['tl']} | Dist: {cz['distance_pct']}%"
        )
        img = _render_chart(cz)
        send_photo(caption, img)
    send_text("✅ SwingWatch Scan Complete\n🕒 4H Candle Close")

def show_latest():
    snaps = STATE.get("last_confluence", {})
    if not snaps:
        send_text("🎯 [SwingWatch] No cached confluence yet. Next scan at 4H close.")
        return
    lines = ["🤖 <b>Next Move — SwingWatch</b>"]
    for sym in ("BTCUSDT","ETHUSDT"):
        cz = snaps.get(sym)
        if not cz: continue
        dist = cz["distance_pct"]
        side = "above" if dist>=0 else "below"
        lines.append(
            f"\n<b>{sym}</b>\n"
            f"Bias: {'🔻 BEARISH' if cz['direction']=='bearish' else '🟢 BULLISH'} reversal\n"
            f"Nearest Zone: {cz['zone_center']:,.0f} ±{cz['zone_width_pct']:.2f}% (Total ${cz['total_usd']:,})\n"
            f"Distance: {dist:+.2f}% {side} price\n"
            f"🎯 Entry: {cz['entry_low']:,.0f} – {cz['entry_high']:,.0f} | ⛔ SL: {cz['stop_loss']:,.0f}"
        )
    send_text("\n".join(lines))
