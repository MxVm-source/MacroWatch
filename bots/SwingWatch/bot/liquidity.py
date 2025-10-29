import os, random
from bot import state

def get_weekly_liquidity_zones():
    # MOCK generator around last mock prices with dynamic drift
    if os.getenv("MOCK_MODE","true").lower() not in ("1","true","yes","on"):
        return []  # real feeds to be wired later
    drift_cfg = state.get_mock_drift()
    out = []
    for sym, base in (("BTCUSDT", state.get_mock_price("BTCUSDT")), ("ETHUSDT", state.get_mock_price("ETHUSDT"))):
        if base is None: 
            continue
        # dynamic drift from env caps
        dmin = float(os.getenv("MOCK_DRIFT_MIN", drift_cfg.get("min",3)))
        dmax = float(os.getenv("MOCK_DRIFT_MAX", drift_cfg.get("max",8)))
        drift_pct = random.uniform(dmin, dmax)
        direction = random.choice([-1, 1])
        price = base * (1 + direction * drift_pct/100.0)
        state.set_mock_price(sym, price)  # update stored price
        total = random.randint(150_000_000, 600_000_000) if sym=="BTCUSDT" else random.randint(100_000_000, 400_000_000)
        bin_part = int(total * random.uniform(0.45, 0.65))
        byb_part = total - bin_part
        out.append({
            "symbol": sym,
            "price": price,
            "zone_center": round(price,2),
            "total_usd": total,
            "binance_usd": bin_part,
            "bybit_usd": byb_part
        })
    return out
