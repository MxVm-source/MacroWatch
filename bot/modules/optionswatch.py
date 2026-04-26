# bot/modules/optionswatch.py
"""
OptionsWatch — Weekly Options Expiry Monitor

Fires Thursday 18:00 UTC (heads up) and Friday 07:00 UTC (expiry morning).
Fetches ETH options data from Deribit public API:
  - Total notional expiring
  - Max pain level
  - Distance from current price to max pain

Max pain = price level where most options expire worthless.
Price tends to gravitate toward max pain into expiry.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("optionswatch")

DERIBIT_BASE = "https://www.deribit.com/api/v2"

STATE = {
    "last_alert_utc":  None,
    # Per-asset state — both BTC and ETH tracked
    "btc": {"expiry_str": None, "max_pain": None, "notional": None, "price": None},
    "eth": {"expiry_str": None, "max_pain": None, "notional": None, "price": None},
    # Backwards compat (legacy ETH-only fields, kept so older callers don't break)
    "last_expiry_str": None,
    "last_max_pain":   None,
    "last_notional":   None,
}


def _get_next_friday_expiry() -> str:
    """Returns next Friday date in Deribit format e.g. '25APR26'"""
    now = datetime.now(timezone.utc)
    days_ahead = (4 - now.weekday()) % 7  # 4 = Friday
    if days_ahead == 0:
        days_ahead = 7
    friday = now + timedelta(days=days_ahead)
    month_map = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                 7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
    return f"{friday.day}{month_map[friday.month]}{str(friday.year)[2:]}"


def _fetch_instruments(expiry: str, asset: str = "ETH") -> list:
    """Fetch all option instruments for given expiry and asset (BTC or ETH).
       Falls back to closest upcoming expiry if exact date not listed."""
    asset = asset.upper()
    all_instruments = []
    for currency in [asset, asset.lower()]:
        try:
            r = requests.get(
                f"{DERIBIT_BASE}/public/get_instruments",
                params={"currency": currency, "kind": "option", "expired": "false"},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"Deribit HTTP {r.status_code} for currency={currency}: {r.text[:200]}")
                continue
            data = r.json()
            if "error" in data:
                log.warning(f"Deribit API error for currency={currency}: {data['error']}")
                continue
            instruments = data.get("result") or []
            if instruments:
                log.info(f"Deribit: got {len(instruments)} {asset} instruments")
                all_instruments = instruments
                break
        except requests.exceptions.Timeout:
            log.warning(f"Deribit timeout for currency={currency}")
        except Exception as e:
            log.warning(f"Deribit fetch error for currency={currency}: {type(e).__name__}: {e}")

    if not all_instruments:
        return []

    # Filter to asset-only (instrument names start with asset symbol)
    asset_instruments = [i for i in all_instruments
                         if i.get("instrument_name", "").startswith(asset)]

    # Try exact expiry match
    matched = [i for i in asset_instruments if expiry in i.get("instrument_name", "")]
    if matched:
        log.info(f"Deribit: {len(matched)} {asset} instruments match expiry {expiry}")
        return matched

    # Fallback: closest upcoming expiry
    from datetime import datetime as dt, timezone as tz
    now_ms = int(dt.now(tz.utc).timestamp() * 1000)
    upcoming = [i for i in asset_instruments if i.get("expiration_timestamp", 0) > now_ms]
    if not upcoming:
        log.warning(f"No upcoming {asset} expiries found on Deribit")
        return []

    upcoming.sort(key=lambda i: i.get("expiration_timestamp", 0))
    closest_expiry = upcoming[0].get("expiration_timestamp", 0)
    closest_group = [i for i in upcoming if i.get("expiration_timestamp") == closest_expiry]
    log.info(f"OptionsWatch {asset}: exact expiry '{expiry}' not found, using closest: {len(closest_group)} instruments")
    return closest_group


def _fetch_ticker(instrument: str) -> dict | None:
    try:
        r = requests.get(
            f"{DERIBIT_BASE}/public/ticker",
            params={"instrument_name": instrument},
            timeout=6,
        )
        data = r.json()
        return data.get("result")
    except Exception:
        return None


def _compute_max_pain(instruments: list) -> dict | None:
    """
    Compute max pain by finding the strike where total option value (calls + puts)
    is minimized — i.e. where most options expire worthless.
    """
    import time

    strikes = {}
    total_notional = 0

    for inst in instruments[:60]:  # cap to avoid rate limits
        name   = inst.get("instrument_name", "")
        strike = inst.get("strike")
        opt_type = "call" if "-C" in name else "put"

        if not strike:
            continue

        ticker = _fetch_ticker(name)
        time.sleep(0.05)  # gentle rate limiting

        if not ticker:
            continue

        oi    = ticker.get("open_interest") or 0
        price = ticker.get("underlying_price") or ticker.get("mark_price") or 0

        if oi > 0 and price > 0:
            notional = oi * price
            total_notional += notional

            if strike not in strikes:
                strikes[strike] = {"calls": 0, "puts": 0}
            strikes[strike][opt_type + "s"] += oi

    if not strikes:
        return None

    # For each strike, compute total pain if price expires there
    # Call pain at S = sum of (S - K) * OI for all calls with K < S
    # Put pain at S  = sum of (K - S) * OI for all puts with K > S
    min_pain   = float("inf")
    max_pain_s = None

    all_strikes = sorted(strikes.keys())

    for s in all_strikes:
        call_pain = sum(
            max(0, s - k) * v["calls"]
            for k, v in strikes.items()
        )
        put_pain = sum(
            max(0, k - s) * v["puts"]
            for k, v in strikes.items()
        )
        total = call_pain + put_pain
        if total < min_pain:
            min_pain   = total
            max_pain_s = s

    return {
        "max_pain":       max_pain_s,
        "total_notional": total_notional,
        "strikes":        len(strikes),
    }


def run_analysis(asset: str = "ETH") -> dict | None:
    asset = asset.upper()
    expiry = _get_next_friday_expiry()
    log.info(f"OptionsWatch {asset}: analysing expiry {expiry}...")

    instruments = _fetch_instruments(expiry, asset=asset)
    if not instruments:
        log.warning(f"OptionsWatch {asset}: no instruments found for {expiry}")
        return None

    result = _compute_max_pain(instruments)
    if not result:
        return None

    result["expiry"] = expiry
    result["asset"]  = asset
    return result


def _fetch_spot_price(asset: str) -> float | None:
    """Fetch current spot price from Bitget."""
    symbol = f"{asset.upper()}USDT"
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")},
            timeout=6,
        )
        d = r.json().get("data") or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        price = float(d.get("lastPr") or d.get("last") or 0)
        return price if price > 0 else None
    except Exception:
        return None


def send_alert(result: dict, is_morning: bool = False):
    now       = datetime.now(timezone.utc)
    asset     = result.get("asset", "ETH")
    expiry    = result["expiry"]
    max_pain  = result["max_pain"]
    notional  = result["total_notional"]

    spot_price = _fetch_spot_price(asset)

    notional_b = notional / 1e9
    notional_str = f"${notional_b:.2f}B" if notional_b >= 1 else f"${notional/1e6:.0f}M"

    lines = [
        f"⚙️ *OptionsWatch {asset} — {'Expiry Day' if is_morning else 'Pre-Expiry Alert'}*",
        f"Expiry: *{expiry}* (Friday 08:00 UTC)",
        "",
        f"Notional expiring: `{notional_str}`",
        f"Max Pain: `${max_pain:,.0f}`",
    ]

    if spot_price and max_pain:
        gap     = spot_price - max_pain
        gap_pct = gap / spot_price * 100
        dir_str = "above" if gap > 0 else "below"
        lines += [
            f"{asset} now:  `${spot_price:,.2f}`",
            f"Gap: `${abs(gap):,.0f}` {dir_str} max pain ({abs(gap_pct):.1f}%)",
            "",
        ]
        if abs(gap_pct) > 5:
            if gap > 0:
                lines.append(f"_{asset} is above max pain — gravitational pull downward into expiry._")
            else:
                lines.append(f"_{asset} is below max pain — gravitational pull upward into expiry._")
            lines.append("_Watch for drift toward max pain as expiry approaches._ 🎯")
        else:
            lines.append("_Price near max pain — minimal gravitational pressure. Expiry likely quiet._ ✅")
    else:
        lines.append("_Max pain acts as a magnet — price tends to drift toward it into expiry._")

    lines += ["", f"_Time (UTC): {now.strftime('%Y-%m-%d %H:%M')}_"]

    send_text("\n".join(lines))

    # Update per-asset state
    asset_key = asset.lower()
    if asset_key in STATE:
        STATE[asset_key]["expiry_str"] = expiry
        STATE[asset_key]["max_pain"]   = max_pain
        STATE[asset_key]["notional"]   = notional
        STATE[asset_key]["price"]      = spot_price

    # Backwards compat (legacy ETH fields)
    if asset == "ETH":
        STATE["last_expiry_str"] = expiry
        STATE["last_max_pain"]   = max_pain
        STATE["last_notional"]   = notional

    STATE["last_alert_utc"] = now
    log.info(f"OptionsWatch {asset}: alert sent — {expiry}, max pain ${max_pain:,.0f}")


def refresh_state():
    """
    Silently refresh STATE for BOTH BTC and ETH so IntelWatch always has fresh data.
    Called on a timer — does not send alerts.
    """
    for asset in ["BTC", "ETH"]:
        try:
            result = run_analysis(asset=asset)
            if not result:
                log.debug(f"OptionsWatch {asset} refresh: no data")
                continue
            asset_key = asset.lower()
            STATE[asset_key]["expiry_str"] = result.get("expiry")
            STATE[asset_key]["max_pain"]   = result.get("max_pain")
            STATE[asset_key]["notional"]   = result.get("total_notional")
            STATE[asset_key]["price"]      = _fetch_spot_price(asset)

            # Backwards compat for ETH
            if asset == "ETH":
                STATE["last_expiry_str"] = result.get("expiry")
                STATE["last_max_pain"]   = result.get("max_pain")
                STATE["last_notional"]   = result.get("total_notional")

            log.info(f"OptionsWatch {asset} STATE refreshed: expiry={result.get('expiry')} max_pain=${result.get('max_pain',0):,.0f}")
        except Exception as e:
            log.warning(f"OptionsWatch {asset} refresh failed: {e}")


def run_thursday():
    """Called Thursday 18:00 UTC — pre-expiry heads up for both BTC and ETH."""
    for asset in ["BTC", "ETH"]:
        result = run_analysis(asset=asset)
        if result:
            send_alert(result, is_morning=False)
        else:
            log.warning(f"OptionsWatch {asset}: Thursday alert skipped — no data")


def run_friday():
    """Called Friday 07:00 UTC — expiry morning alert for both BTC and ETH."""
    for asset in ["BTC", "ETH"]:
        result = run_analysis(asset=asset)
        if result:
            send_alert(result, is_morning=True)
        else:
            log.warning(f"OptionsWatch {asset}: Friday alert skipped — no data")


def show_diag():
    lines = ["⚙️ *OptionsWatch Diagnostics*", ""]
    last = STATE["last_alert_utc"]
    lines.append(f"Last alert: {last.strftime('%Y-%m-%d %H:%M UTC') if last else 'Never'}")
    if STATE["last_expiry_str"]:
        lines.append(f"Last expiry: {STATE['last_expiry_str']}")
        lines.append(f"Max pain: ${STATE['last_max_pain']:,.0f}" if STATE["last_max_pain"] else "Max pain: —")
        notional = STATE["last_notional"]
        if notional:
            nb = notional / 1e9
            lines.append(f"Notional: {'${:.2f}B'.format(nb) if nb >= 1 else '${:.0f}M'.format(notional/1e6)}")
    lines.append("\nSchedule: Thu 18:00 UTC + Fri 07:00 UTC")
    send_text("\n".join(lines))
