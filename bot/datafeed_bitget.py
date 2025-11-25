import requests

BITGET_BASE = "https://api.bitget.com/api/mix/v1/market/ticker"

def get_current_position():
    # calls Bitget API
    # returns dict with entry, size, tps, sl, pnl...

def get_ticker(symbol: str):
    """Return last price as float for a Bitget contract symbol, or None on error."""
    try:
        resp = requests.get(BITGET_BASE, params={"symbol": symbol}, timeout=5)
        if not resp.ok:
            print("Bitget error:", resp.status_code, resp.text)
            return None
        data = resp.json()
        if data.get("data") and isinstance(data["data"], dict):
            return float(data["data"].get("last", 0.0))
    except Exception as e:
        print("Bitget exception:", e)
    return None
