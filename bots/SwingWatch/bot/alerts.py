import requests, os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else None

def send_message(text):
    if not TG or not CHAT_ID: 
        return
    try:
        requests.post(f"{TG}/sendMessage", data={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=15)
    except Exception:
        pass

def send_photo(caption, filepath):
    if not TG or not CHAT_ID or not filepath:
        return
    try:
        with open(filepath, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
            requests.post(f"{TG}/sendPhoto", data=data, files=files, timeout=30)
    except Exception:
        pass

def send_confluence_setup(cz):
    breakdown = ""
    if cz.get("binance_usd") is not None and cz.get("bybit_usd") is not None:
        breakdown = f" (Bin ${cz['binance_usd']:,} | Byb ${cz['bybit_usd']:,})"
    text = (
        f"🔔 Confluence Reversal Setup {cz.get('emoji','')} {cz.get('bias','').upper()}\n"
        f"Symbol: {cz.get('symbol')} | TF: 1W + {cz.get('tl_tf','4H')} TL\n"
        f"Liquidity Zone: {cz.get('zone_center'):,.0f} ± {cz.get('zone_width_pct',0):.2f}%\n"
        f"Total: ${cz.get('total_usd',0):,}{breakdown}\n\n"
        f"🎯 Entry Zone: {cz.get('entry_low',0):,.0f} – {cz.get('entry_high',0):,.0f}\n"
        f"⛔ SL: {cz.get('stop_loss',0):,.0f} (±1.00%)\n\n"
        f"Bias: Reversal {cz.get('direction','').upper()}"
    )
    # Optional image
    if os.getenv("POST_IMAGE_ON_SCAN", "true").lower() in ("1","true","yes","on"):
        try:
            from bot.confluence import render_chart
            pth = render_chart(cz)
            if pth:
                send_photo(text, pth)
                return
        except Exception:
            # fallback to text
            pass
    send_message(text)
