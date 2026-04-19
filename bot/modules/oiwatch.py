# bot/modules/oiwatch.py
"""
OIWatch — Open Interest Spike Monitor

Fires when OI changes significantly in a short window on ETH/BNB/SOL.
OI spike + price rising = trend strengthening (bullish conviction)
OI spike + price falling = capitulation / forced liquidations
OI dropping + price rising = short covering rally

Polls every 30 minutes. Cooldown 4h per asset.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("oiwatch")

BITGET_BASE  = "https://api.bitget.com"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

ASSETS  = ["ETHUSDT", "BNBUSDT", "SOLUSDT"]
TICKERS = {"ETHUSDT": "ETH", "BNBUSDT": "BNB", "SOLUSDT": "SOL"}

OI_SPIKE_PCT   = 5.0   # % change in OI to trigger alert
COOLDOWN_MIN   = 240   # 4 hours per asset

STATE = {
    "last_check":  None,
    "last_alert":  {},   # { symbol: datetime }
    "prev_oi":     {},   # { symbol: float }
    "prev_price":  {},   # { symbol: float }
    "last_oi":     {},   # { symbol: float }
    "last_price":  {},   # { symbol: float }
}


def _fetch_oi(symbol: str) -> tuple[float, float] | None:
    """Returns (oi_usdt, price) or None."""
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/open-interest",
            params={"symbol": symbol, "productType": PRODUCT_TYPE},
            timeout=6,
        )
        data = r.json()
        if data.get("code") != "00000":
            log.warning(f"OI API returned non-OK for {symbol}: code={data.get('code')} msg={data.get('msg')}")
            return None
        d = data.get("data") or {}
        if isinstance(d, list):
            d = d[0] if d else {}

        # Bitget V2 current structure: data.openInterestList[0].size (in base currency)
        oi = 0.0
        oi_list = d.get("openInterestList") or []
        if isinstance(oi_list, list) and oi_list:
            size_raw = oi_list[0].get("size") or oi_list[0].get("openInterest") or 0
            try:
                oi = float(size_raw)
            except Exception:
                oi = 0.0

        # Fallback: legacy flat fields
        if oi == 0.0:
            oi_raw = (d.get("openInterestValue")
                      or d.get("openInterest")
                      or d.get("amount")
                      or d.get("size")
                      or 0)
            try:
                oi = float(oi_raw)
            except Exception:
                oi = 0.0

        # Get price from ticker (not included in OI response)
        price = 0.0
        try:
            tr = requests.get(
                f"{BITGET_BASE}/api/v2/mix/market/ticker",
                params={"symbol": symbol, "productType": PRODUCT_TYPE},
                timeout=5,
            )
            tdata = tr.json()
            if tdata.get("code") == "00000":
                td = tdata.get("data") or {}
                if isinstance(td, list):
                    td = td[0] if td else {}
                price = float(td.get("lastPr") or td.get("last") or td.get("markPrice") or 0)
        except Exception as e:
            log.warning(f"Ticker fetch failed for {symbol}: {e}")

        # OI is in base currency — convert to USDT
        if oi > 0 and price > 0:
            oi_usdt = oi * price
        else:
            oi_usdt = 0.0

        if oi_usdt <= 0:
            log.warning(f"OI={oi_usdt} for {symbol} — base OI={oi}, price={price}")
            return None

        return (oi_usdt, price)
    except Exception as e:
        log.warning(f"OI fetch failed for {symbol}: {e}")
        return None


def _cooldown_ok(symbol: str) -> bool:
    last = STATE["last_alert"].get(symbol)
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(minutes=COOLDOWN_MIN)


def _interpret(oi_chg: float, price_chg: float) -> tuple[str, str]:
    """Returns (emoji, interpretation text)"""
    if oi_chg > 0 and price_chg > 0:
        return "📈", "OI rising + price rising — trend strengthening. Bulls adding conviction."
    elif oi_chg > 0 and price_chg < 0:
        return "⚠️", "OI rising + price falling — bearish pressure building. Shorts piling in."
    elif oi_chg < 0 and price_chg > 0:
        return "🚀", "OI dropping + price rising — short squeeze in play. Bears covering fast."
    elif oi_chg < 0 and price_chg < 0:
        return "📉", "OI dropping + price falling — capitulation. Longs getting wrecked."
    return "➡️", "Mixed signals — monitor closely."


def poll_once():
    now = datetime.now(timezone.utc)
    STATE["last_check"] = now

    spikes = []

    for sym in ASSETS:
        result = _fetch_oi(sym)
        if result is None:
            continue

        oi, price = result
        prev_oi    = STATE["prev_oi"].get(sym)
        prev_price = STATE["prev_price"].get(sym)

        STATE["last_oi"][sym]    = oi
        STATE["last_price"][sym] = price

        if prev_oi and prev_oi > 0:
            oi_chg_pct    = (oi - prev_oi) / prev_oi * 100
            price_chg_pct = (price - prev_price) / prev_price * 100 if prev_price else 0

            if abs(oi_chg_pct) >= OI_SPIKE_PCT and _cooldown_ok(sym):
                spikes.append({
                    "sym":         sym,
                    "ticker":      TICKERS[sym],
                    "oi":          oi,
                    "prev_oi":     prev_oi,
                    "oi_chg":      oi_chg_pct,
                    "price":       price,
                    "prev_price":  prev_price,
                    "price_chg":   price_chg_pct,
                })

        STATE["prev_oi"][sym]    = oi
        STATE["prev_price"][sym] = price

    if not spikes:
        return

    for spike in spikes:
        oi_b      = spike["oi"] / 1e9
        prev_oi_b = spike["prev_oi"] / 1e9
        oi_sign   = "+" if spike["oi_chg"] >= 0 else ""
        px_sign   = "+" if spike["price_chg"] >= 0 else ""
        interp_e, interp_text = _interpret(spike["oi_chg"], spike["price_chg"])

        lines = [
            f"📊 *OIWatch — Spike Detected*",
            f"Asset: *{spike['ticker']}*",
            "",
            f"OI:    `${prev_oi_b:.2f}B → ${oi_b:.2f}B` ({oi_sign}{spike['oi_chg']:.1f}%) 🔥",
            f"Price: `${spike['prev_price']:,.2f} → ${spike['price']:,.2f}` ({px_sign}{spike['price_chg']:.2f}%)",
            "",
            f"{interp_e} _{interp_text}_",
            "",
            f"_Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}_",
        ]
        send_text("\n".join(lines))
        STATE["last_alert"][spike["sym"]] = now
        log.info(f"OIWatch: spike alert fired for {spike['sym']} — OI {spike['oi_chg']:+.1f}%")


def show_diag():
    lines = ["📊 *OIWatch Diagnostics*", ""]
    last = STATE["last_check"]
    lines.append(f"Last check: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}")
    lines.append("")
    lines.append("*Current OI:*")
    for sym in ASSETS:
        oi    = STATE["last_oi"].get(sym)
        price = STATE["last_price"].get(sym)
        ticker = TICKERS[sym]
        if oi:
            lines.append(f"  {ticker}: `${oi/1e9:.2f}B` @ `${price:,.2f}`")
        else:
            lines.append(f"  {ticker}: —")
    lines.append(f"\nSpike threshold: ±{OI_SPIKE_PCT}%")
    send_text("\n".join(lines))
