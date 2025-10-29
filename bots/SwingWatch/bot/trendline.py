import random

def get_strong_trendlines():
    # MOCK: one ascending (support) and one descending (resistance) per symbol
    tlines = []
    for sym in ("BTCUSDT","ETHUSDT"):
        # Resistance
        tlines.append({
            "symbol": sym, "tf": "4H", "type": "Descending Resistance",
            "touches": random.randint(3,5), "slope": -random.uniform(0.1, 0.6)
        })
        # Support
        tlines.append({
            "symbol": sym, "tf": "4H", "type": "Ascending Support",
            "touches": random.randint(3,5), "slope": random.uniform(0.1, 0.6)
        })
    return tlines
