import os, time, requests
from bot import state

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TG = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

def _send(chat_id, text):
    if not TG or not chat_id: return
    try:
        requests.post(f"{TG}/sendMessage", data={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=15)
    except Exception:
        pass

def _format_line(snap):
    if not snap: return "No confluence cached yet."
    is_bear = snap.get("direction") == "bearish"
    arrow = "🔻 BEARISH" if is_bear else "🟢 BULLISH"
    over_under = "overhead" if is_bear else "underfoot"
    dist = snap.get("distance_pct")
    if dist is None:
        dist_txt, side = "n/a", ""
    else:
        dist_txt = f"{'+' if dist>=0 else ''}{dist:.2f}%"
        side = "above" if dist>=0 else "below"
    total_usd = snap.get("total_usd",0)
    breakdown = ""
    if snap.get("binance_usd") is not None and snap.get("bybit_usd") is not None:
        breakdown = f" (Bin ${snap['binance_usd']:,} | Byb ${snap['bybit_usd']:,})"
    return (
        f"<b>{snap['symbol']}</b>\n"
        f"Bias: {arrow} reversal ({over_under})\n"
        f"Nearest Zone: {snap['zone_center']:,.0f} ±{snap.get('zone_width_pct',0):.2f}% (Total ${total_usd:,}){breakdown}\n"
        f"Distance: {dist_txt} {side} price\n"
        f"🎯 Entry: {snap['entry_low']:,.0f} – {snap['entry_high']:,.0f}\n"
        f"⛔ SL: {snap['stop_loss']:,.0f} (±1.00%)"
    )

def _build_next():
    snaps = state.get_snapshot().get("symbols", {})
    parts = ["🤖 <b>Next Move — SwingWatch</b>"]
    for sym in ("BTCUSDT","ETHUSDT"):
        if sym in snaps:
            parts.append("\n" + _format_line(snaps[sym]))
    if len(parts) == 1:
        parts.append("\nNo cached confluence yet. Next scan at the 4H close.")
    return "\n".join(parts)

def run_command_loop():
    if not TG or not CHAT_ID: 
        return
    offset = None
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates", params={"timeout": 20, "offset": offset}, timeout=25)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip().lower()
                chat = msg.get("chat", {}).get("id")
                if not text or str(chat) != str(CHAT_ID):
                    continue
                if text.startswith("/next") or text.startswith("/nextmove"):
                    _send(CHAT_ID, _build_next())
        except Exception:
            time.sleep(2)
