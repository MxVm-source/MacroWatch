# bot/modules/liquidation.py
import os, time, random, requests

THRESHOLD = int(os.getenv("LIQ_THRESHOLD_USD", "150000000"))
PROX_PCT  = float(os.getenv("LIQ_PROXIMITY_PCT", "0.6"))

PROVIDER   = os.getenv("LIQ_PROVIDER", "mock").lower()   # mock | http
API_URL    = os.getenv("LIQ_API_URL", "")               # e.g. CoinGlass/Hyblock endpoint
API_KEY    = os.getenv("LIQ_API_KEY", "")               # put your key here
API_KEY_HDR= os.getenv("LIQ_API_KEY_HEADER", "X-API-KEY")  # header name if needed
ARR_PATH   = os.getenv("LIQ_ARRAY_PATH", "")            # dot path to the list in JSON (e.g. "data.items")
PRICE_FLD  = os.getenv("LIQ_PRICE_FIELD", "price")      # field name for price
USD_FLD    = os.getenv("LIQ_USD_FIELD", "usd")          # field name for notional ($)

# Manual clusters (optional)
_MANUAL = {"BTCUSDT": [], "ETHUSDT": []}

def add_manual(symbol: str, price: float, size_usd: int, ttl_sec: int = 86400):
    _MANUAL.setdefault(symbol, []).append(
        {"price": float(price), "usd": int(size_usd), "exp": time.time() + ttl_sec}
    )

def _clean_manual():
    now = time.time()
    for sym, arr in _MANUAL.items():
        _MANUAL[sym] = [x for x in arr if x.get("exp", now+1) > now]

def _mock(symbol: str, spot_price: float):
    out = []
    for _ in range(3):
        drift = random.uniform(-1.5, 1.5) / 100.0
        lvl = spot_price * (1 + drift)
        size = random.randint(120_000_000, 600_000_000) if symbol == "BTCUSDT" else random.randint(90_000_000, 350_000_000)
        out.append({"price": round(lvl, 2), "usd": size})
    return out

def _get_nested(obj, path: str):
    # very small dot-path extractor: "data.items" -> obj["data"]["items"]
    cur = obj
    if not path:
        return cur
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur

def _http_fetch(symbol: str):
    """
    Generic HTTP provider:
      - GET LIQ_API_URL (we append ?symbol=SYMBOL if it contains '{symbol}')
      - Header: API_KEY_HDR: API_KEY (if provided)
      - Extract array via ARR_PATH (dot-path)
      - Map each item using PRICE_FLD / USD_FLD to {"price","usd"}
    """
    if not API_URL:
        return []

    url = API_URL.replace("{symbol}", symbol)  # optional templating
    headers = {}
    if API_KEY:
        headers[API_KEY_HDR] = API_KEY

    try:
        r = requests.get(url, headers=headers, timeout=8)
        j = r.json()
    except Exception:
        return []

    arr = _get_nested(j, ARR_PATH) if ARR_PATH else j
    if not isinstance(arr, list):
        return []

    out = []
    for it in arr:
        try:
            price = float(it[PRICE_FLD])
            usd   = float(it[USD_FLD])
            out.append({"price": price, "usd": int(usd)})
        except Exception:
            continue
    return out

def get_clusters(symbol: str, spot_price: float, use_mock: bool = False):
    """
    Returns [{"price": float, "usd": int}, ...] filtered by THRESHOLD.
    symbol: "BTCUSDT" / "ETHUSDT"
    """
    _clean_manual()
    items = []

    if PROVIDER == "http":
        items.extend(_http_fetch(symbol))
        # optional fallback to mock if nothing returned
        if not items and use_mock:
            items.extend(_mock(symbol, spot_price))
    else:
        items.extend(_mock(symbol, spot_price))  # default mock

    items.extend(_MANUAL.get(symbol, []))
    items = [x for x in items if int(x["usd"]) >= THRESHOLD]
    # sort by: closer to price, then larger size
    items.sort(key=lambda x: (abs(x["price"] - spot_price), -x["usd"]))
    return items

def is_confluent(spot_price: float, level_price: float) -> bool:
    if not level_price:
        return False
    pct = abs(level_price - spot_price) / max(1e-9, float(spot_price)) * 100.0
    return pct <= PROX_PCT
