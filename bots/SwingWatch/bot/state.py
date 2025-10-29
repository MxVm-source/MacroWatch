from threading import Lock

_snap = {"symbols": {}}
_mock = {"BTCUSDT": 113000.0, "ETHUSDT": 3984.0, "drift": {"min": 3, "max": 8}}
_lock = Lock()

def set_snapshot(symbol, data):
    with _lock:
        _snap["symbols"][symbol] = data

def get_snapshot():
    with _lock:
        return dict(_snap)

def get_mock_price(symbol):
    with _lock:
        return _mock.get(symbol)

def set_mock_price(symbol, price):
    with _lock:
        _mock[symbol] = price

def get_mock_drift():
    with _lock:
        return dict(_mock.get("drift", {"min": 3, "max": 8}))

def set_mock_drift(minv, maxv):
    with _lock:
        _mock["drift"] = {"min": float(minv), "max": float(maxv)}
