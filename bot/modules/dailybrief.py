# bot/modules/dailybrief.py
"""
dailybrief.py — Private daily intelligence briefings.

Two fires per day, private group only:
  Morning brief  08:00 Brussels — "what to watch today"
  Evening recap  21:00 Brussels — "what happened today"

Both are structured cards (no AI generation — fast, reliable, no API cost).
The evening recap uses a lightweight AI summary call if OPENAI_API_KEY is set,
otherwise falls back to a structured card.
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta

from bot.utils import send_text

log = logging.getLogger("dailybrief")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BRUSSELS_TZ    = "Europe/Brussels"

# Track today's alert counts (reset at midnight UTC)
_daily_state: dict = {
    "date":           None,   # YYYY-MM-DD
    "trump_count":    0,
    "fedwatch_fired": [],     # event titles that fired today
    "btc_open":       None,   # BTC price at morning brief
    "eth_open":       None,
}


def _reset_if_new_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_state["date"] != today:
        _daily_state.update({
            "date":           today,
            "trump_count":    0,
            "fedwatch_fired": [],
            "btc_open":       None,
            "eth_open":       None,
        })


def record_trump_alert():
    """Called by TrumpWatch when an alert fires — increments today's count."""
    _reset_if_new_day()
    _daily_state["trump_count"] += 1


def record_fedwatch_event(title: str):
    """Called by FedWatch when an alert fires — logs the event title."""
    _reset_if_new_day()
    if title not in _daily_state["fedwatch_fired"]:
        _daily_state["fedwatch_fired"].append(title)


def _fetch_price(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
            timeout=10,
        )
        data = r.json().get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return float(data.get("lastPr") or data.get("close") or 0) or None
    except Exception as e:
        log.warning(f"price fetch failed for {symbol}: {e}")
        return None


def _fetch_funding(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/current-fund-rate",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
            timeout=10,
        )
        data = r.json().get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return float(data.get("fundingRate") or 0) or None
    except Exception:
        return None


def _regime_label(modules: dict) -> str:
    """Quick regime from CorrelWatch / price context — BULL / BEAR / CHOP."""
    try:
        import numpy as np
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params={"symbol": "BTCUSDT", "granularity": "4H",
                    "limit": "210", "productType": "USDT-FUTURES"},
            timeout=10,
        )
        data = r.json().get("data") or []
        if len(data) < 200:
            return "UNKNOWN"
        closes = [float(c[4]) for c in sorted(data, key=lambda x: int(x[0]))]
        spot   = closes[-1]
        sma50  = float(np.mean(closes[-50:]))
        sma200 = float(np.mean(closes[-200:]))
        if spot > sma50 and spot > sma200:
            return "BULL"
        elif spot < sma50 and spot < sma200:
            return "BEAR"
        else:
            return "CHOP"
    except Exception:
        return "UNKNOWN"


def _regime_emoji(r: str) -> str:
    return {"BULL": "🟢", "BEAR": "🔴", "CHOP": "⚪", "UNKNOWN": "⚪"}.get(r, "⚪")


def _events_today(modules: dict) -> list:
    """High-impact events firing TODAY (UTC)."""
    try:
        HIGH   = {"FOMC", "CPI", "NFP", "ECB", "PPI"}
        events = modules["fedwatch"].STATE.get("events", [])
        now    = datetime.now(timezone.utc)
        today  = now.date()
        return [
            ev for ev in events
            if ev.get("category") in HIGH
            and ev.get("start")
            and ev["start"].date() == today
        ]
    except Exception:
        return []


def _events_next_7(modules: dict) -> list:
    """High-impact events in the next 7 days."""
    try:
        HIGH   = {"FOMC", "CPI", "NFP", "ECB", "PPI"}
        events = modules["fedwatch"].STATE.get("events", [])
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=7)
        upcoming = [
            ev for ev in events
            if ev.get("category") in HIGH
            and ev.get("start")
            and now < ev["start"] <= cutoff
        ]
        return sorted(upcoming, key=lambda e: e["start"])[:3]
    except Exception:
        return []


def _vix_line(modules: dict) -> str | None:
    try:
        vix = modules["vixwatch"].STATE.get("last_vix")
        if not vix:
            return None
        zone = modules["vixwatch"]._get_zone(vix)
        return f"😱 VIX: {vix:.1f} {zone.get('emoji', '')} {zone.get('label', '')}"
    except Exception:
        return None


def _ai_summary(context: str) -> str | None:
    """Optional AI narrative for evening recap. Falls back gracefully."""
    if not OPENAI_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model":       OPENAI_MODEL,
                "max_tokens":  120,
                "temperature": 0.4,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Write a 2-sentence plain-English summary of today's crypto market "
                        "for a private investor group. No jargon. No hype. Factual only.\n\n"
                        f"Context:\n{context}"
                    ),
                }],
            },
            timeout=20,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"AI summary failed: {e}")
        return None


# ─── Morning Brief ────────────────────────────────────────────────────────────

def send_morning_brief(modules: dict):
    """08:00 Brussels — what to watch today."""
    _reset_if_new_day()

    btc = _fetch_price("BTCUSDT")
    eth = _fetch_price("ETHUSDT")

    # Store opening prices for evening comparison
    _daily_state["btc_open"] = btc
    _daily_state["eth_open"] = eth

    btc_f = f"${btc:,.0f}" if btc else "N/A"
    eth_f = f"${eth:,.2f}" if eth else "N/A"

    funding_btc = _fetch_funding("BTCUSDT")
    f_btc = f"{funding_btc*100:.4f}%/8h" if funding_btc is not None else "N/A"
    f_apr = funding_btc * 3 * 365 * 100 if funding_btc is not None else None
    f_crowd = ""
    if f_apr is not None:
        if f_apr > 8:
            f_crowd = " ⚠️ crowded LONG"
        elif f_apr < -8:
            f_crowd = " ⚠️ crowded SHORT"

    regime  = _regime_label(modules)
    r_emoji = _regime_emoji(regime)

    now      = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")

    lines = [
        f"🌅 *Good morning — {date_str}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"₿ BTC: `{btc_f}`",
        f"Ξ ETH: `{eth_f}`",
        f"Regime: {r_emoji} *{regime}*",
        f"💸 Funding: `{f_btc}`{f_crowd}",
    ]

    # VIX
    vix = _vix_line(modules)
    if vix:
        lines.append(vix)

    # Today's macro events
    today_events = _events_today(modules)
    if today_events:
        lines.append("")
        lines.append("📅 *On the calendar today*")
        for ev in today_events:
            t = ev["start"].strftime("%H:%M UTC")
            lines.append(f"  · {ev['title']} @ {t}")
    else:
        # Show next upcoming event if nothing today
        upcoming = _events_next_7(modules)
        if upcoming:
            next_ev  = upcoming[0]
            days_away = (next_ev["start"].date() - now.date()).days
            t        = next_ev["start"].strftime("%b %d")
            lines.append("")
            lines.append(f"📅 Next: {next_ev['title']} in {days_away}d ({t})")
        else:
            lines.append("")
            lines.append("📅 No major macro events this week")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Eyes open. Let the levels come to you._",
    ]

    send_text("\n".join(lines))
    log.info("Morning brief sent ✅")


# ─── Evening Recap ────────────────────────────────────────────────────────────

def send_evening_recap(modules: dict):
    """21:00 Brussels — what happened today."""
    _reset_if_new_day()

    btc_now = _fetch_price("BTCUSDT")
    eth_now = _fetch_price("ETHUSDT")
    btc_open = _daily_state.get("btc_open")
    eth_open = _daily_state.get("eth_open")

    now      = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")

    def _chg(now, open_):
        if not now or not open_:
            return None
        return (now - open_) / open_ * 100

    btc_chg = _chg(btc_now, btc_open)
    eth_chg = _chg(eth_now, eth_open)

    def _fmt_price(price, chg):
        if not price:
            return "N/A"
        chg_s = f" ({'+' if chg >= 0 else ''}{chg:.2f}%)" if chg is not None else ""
        sym   = "₿" if "BTC" in str(price) else ""
        return f"${price:,.0f}{chg_s}" if price > 1000 else f"${price:,.2f}{chg_s}"

    btc_f = f"${btc_now:,.0f}{f' ({btc_chg:+.2f}%)' if btc_chg is not None else ''}" if btc_now else "N/A"
    eth_f = f"${eth_now:,.2f}{f' ({eth_chg:+.2f}%)' if eth_chg is not None else ''}" if eth_now else "N/A"

    regime  = _regime_label(modules)
    r_emoji = _regime_emoji(regime)

    # Notable move flag
    notable = ""
    if btc_chg is not None and abs(btc_chg) >= 3:
        notable = f"⚡ Notable: BTC moved {btc_chg:+.1f}% today"

    # Trump alerts today
    trump_count = _daily_state.get("trump_count", 0)
    trump_line  = f"🍊 TrumpWatch: {trump_count} alert{'s' if trump_count != 1 else ''} today" if trump_count else "🍊 TrumpWatch: quiet"

    # Fed events today
    fed_fired = _daily_state.get("fedwatch_fired", [])
    fed_line  = f"🏦 FedWatch: {', '.join(fed_fired)}" if fed_fired else "🏦 FedWatch: no events"

    # Funding
    funding_btc = _fetch_funding("BTCUSDT")
    f_btc = f"{funding_btc*100:.4f}%/8h" if funding_btc is not None else "N/A"

    # Build context for AI summary
    context = (
        f"BTC: {btc_f}, ETH: {eth_f}, Regime: {regime}, "
        f"Funding: {f_btc}, {trump_line}, {fed_line}"
    )
    if notable:
        context += f", {notable}"

    ai = _ai_summary(context)

    lines = [
        f"🌙 *Evening Recap — {date_str}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"₿ BTC: `{btc_f}`",
        f"Ξ ETH: `{eth_f}`",
        f"Regime: {r_emoji} *{regime}*",
        f"💸 Funding: `{f_btc}`",
    ]

    if notable:
        lines.append(notable)

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        trump_line,
        fed_line,
    ]

    # VIX
    vix = _vix_line(modules)
    if vix:
        lines.append(vix)

    if ai:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "*Today in one paragraph*",
            ai,
        ]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Session closed. See you tomorrow._",
    ]

    send_text("\n".join(lines))
    log.info("Evening recap sent ✅")
