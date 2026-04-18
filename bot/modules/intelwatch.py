# bot/modules/intelwatch.py
"""
IntelWatch — Full Market Intelligence Briefing

Command: /intel
Auto-trigger: fires to public channel when 2+ signals align simultaneously.

Pulls live data from all modules:
  - FedWatch (next macro event)
  - TrumpWatch (last score + bias)
  - VixWatch (VIX level + regime)
  - Fear & Greed (sentiment)
  - CorrelWatch (DXY vs BTC)
  - FundingWatch (funding rates)
  - OIWatch (open interest)
  - OptionsWatch (max pain)
  - LiquidationWatch (session liqs)
  - S&RWatch (nearest levels)

Scores each signal bull/bear, produces a bias verdict.
Auto-trigger cooldown: 2h (never spams).
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("intelwatch")

PUBLIC_CHAT_ID   = os.getenv("PUBLIC_CHAT_ID", "")
AUTO_COOLDOWN_H  = 2
BITGET_BASE      = "https://api.bitget.com"
PRODUCT_TYPE     = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")

STATE = {
    "last_intel_utc":    None,
    "last_auto_utc":     None,
    "last_score":        None,
    "last_bias":         None,
}

# ─── Auto-trigger conditions ──────────────────────────────────────────────────

AUTO_TRIGGER_RULES = [
    # (condition_name, description)
    ("fedwatch_imminent",   "Major macro event within 3h"),
    ("vix_elevated",        "VIX > 25"),
    ("sr_hit",              "BTC hit key S/R level"),
    ("funding_extreme",     "Extreme funding rate"),
    ("trump_high",          "TrumpWatch score >= 7"),
    ("btc_volatile",        "BTC moving > 2% in 4H"),
    ("correl_diverge",      "DXY/BTC divergence active"),
    ("liq_large",           "Large liquidation this hour"),
]


def _auto_cooldown_ok():
    last = STATE["last_auto_utc"]
    if not last:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=AUTO_COOLDOWN_H)


# ─── BTC price fetch ──────────────────────────────────────────────────────────

def _fetch_btc_price() -> tuple[float, float] | tuple[None, None]:
    """Returns (current_price, 4h_change_pct)"""
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/candles",
            params={"symbol": "BTCUSDT", "granularity": "4H",
                    "limit": "3", "productType": PRODUCT_TYPE},
            timeout=6,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None, None
        candles = data.get("data") or []
        if len(candles) < 2:
            return None, None
        price      = float(candles[-1][4])
        prev_close = float(candles[-2][4])
        chg        = (price - prev_close) / prev_close * 100
        return round(price, 2), round(chg, 2)
    except Exception as e:
        log.warning(f"BTC price fetch failed: {e}")
        return None, None


# ─── Signal evaluator ─────────────────────────────────────────────────────────

def _evaluate_signals(modules: dict) -> dict:
    """
    Pull data from all modules, score each signal.
    Returns dict with signal states and bull/bear scores.
    """
    now    = datetime.now(timezone.utc)
    score  = {"bull": 0, "bear": 0, "neutral": 0}
    signals = {}

    # ── FedWatch
    try:
        fw_events = modules["fedwatch"].STATE.get("events", [])
        next_event = None
        for ev in fw_events:
            start = ev.get("start")
            if start and start > now:
                next_event = ev
                break
        if next_event:
            delta_h = (next_event["start"] - now).total_seconds() / 3600
            impact  = next_event.get("impact", "medium").lower()
            label   = next_event.get("title", "Event")[:40]
            if delta_h <= 3 and impact == "high":
                signals["fedwatch"] = {"text": f"⚠️ {label} in {int(delta_h*60)}m — HIGH IMPACT", "bias": "neutral"}
                score["neutral"] += 1
            elif delta_h <= 24:
                signals["fedwatch"] = {"text": f"📅 {label} in {int(delta_h)}h", "bias": "neutral"}
                score["neutral"] += 1
            else:
                signals["fedwatch"] = {"text": f"📅 Next: {label}", "bias": "neutral"}
        else:
            signals["fedwatch"] = {"text": "📅 No major events upcoming", "bias": "neutral"}
    except Exception as e:
        signals["fedwatch"] = {"text": "📅 FedWatch: unavailable", "bias": "neutral"}

    # ── TrumpWatch
    try:
        tw_state  = modules["trumpwatch"].STATE
        last_score = tw_state.get("last_score") or tw_state.get("last_ai_score")
        last_bias  = tw_state.get("last_bias") or tw_state.get("last_sentiment", "")
        if last_score is not None:
            bear = "bearish" in str(last_bias).lower()
            bull = "bullish" in str(last_bias).lower()
            emoji = "🔴" if bear else "🟢" if bull else "🟡"
            b = "bear" if bear else "bull" if bull else "neutral"
            signals["trump"] = {"text": f"🍊 Score {last_score}/10 {emoji} {str(last_bias).capitalize()}", "bias": b}
            score[b] += 1
        else:
            signals["trump"] = {"text": "🍊 TrumpWatch: no recent alerts", "bias": "neutral"}
    except Exception as e:
        signals["trump"] = {"text": "🍊 TrumpWatch: unavailable", "bias": "neutral"}

    # ── VixWatch
    try:
        vw_state = modules["vixwatch"].STATE
        vix = vw_state.get("last_vix")
        if vix:
            if vix >= 40:
                emoji, b, regime = "🔴", "bear", "EXTREME FEAR"
            elif vix >= 30:
                emoji, b, regime = "🟠", "bear", "HIGH FEAR"
            elif vix >= 20:
                emoji, b, regime = "🟡", "bear", "ELEVATED"
            else:
                emoji, b, regime = "🟢", "bull", "CALM"
            signals["vix"] = {"text": f"😱 VIX: {vix:.1f} {emoji} {regime}", "bias": b}
            score[b] += 1
        else:
            signals["vix"] = {"text": "😱 VIX: unavailable", "bias": "neutral"}
    except Exception as e:
        signals["vix"] = {"text": "😱 VIX: unavailable", "bias": "neutral"}

    # ── CorrelWatch
    try:
        cw_state = modules["correlwatch"].STATE
        dxy = cw_state.get("last_dxy")
        btc = cw_state.get("last_btc")
        if dxy is not None and btc is not None:
            if dxy > 0 and btc < 0:
                signals["correl"] = {"text": f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% 🔴 Bearish pressure", "bias": "bear"}
                score["bear"] += 1
            elif dxy < 0 and btc > 0:
                signals["correl"] = {"text": f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% 🟢 Bullish tailwind", "bias": "bull"}
                score["bull"] += 1
            else:
                signals["correl"] = {"text": f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% ➡️ No divergence", "bias": "neutral"}
                score["neutral"] += 1
        else:
            signals["correl"] = {"text": "📡 CorrelWatch: no data", "bias": "neutral"}
    except Exception as e:
        signals["correl"] = {"text": "📡 CorrelWatch: unavailable", "bias": "neutral"}

    # ── FundingWatch
    try:
        fw_rates = modules["fundingwatch"].STATE.get("last_rates", {})
        eth_rate = fw_rates.get("ETHUSDT")
        btc_rate = fw_rates.get("BTCUSDT") or eth_rate
        if btc_rate is not None:
            if btc_rate >= 0.10:
                signals["funding"] = {"text": f"💸 Funding: {btc_rate:+.4f}% 🔴 Overleveraged longs — squeeze risk", "bias": "bear"}
                score["bear"] += 1
            elif btc_rate <= -0.05:
                signals["funding"] = {"text": f"💸 Funding: {btc_rate:+.4f}% 🔵 Overleveraged shorts — squeeze risk", "bias": "bull"}
                score["bull"] += 1
            elif btc_rate >= 0.05:
                signals["funding"] = {"text": f"💸 Funding: {btc_rate:+.4f}% 🟡 Elevated long bias", "bias": "bear"}
                score["bear"] += 1
            else:
                signals["funding"] = {"text": f"💸 Funding: {btc_rate:+.4f}% ⚪ Neutral", "bias": "neutral"}
                score["neutral"] += 1
        else:
            signals["funding"] = {"text": "💸 Funding: no data", "bias": "neutral"}
    except Exception as e:
        signals["funding"] = {"text": "💸 Funding: unavailable", "bias": "neutral"}

    # ── OIWatch
    try:
        oi_data  = modules["oiwatch"].STATE.get("last_oi", {})
        prev_oi  = modules["oiwatch"].STATE.get("prev_oi", {})
        eth_oi   = oi_data.get("ETHUSDT") or oi_data.get("BTCUSDT")
        prev_eth = prev_oi.get("ETHUSDT") or prev_oi.get("BTCUSDT")
        if eth_oi:
            oi_b = eth_oi / 1e9
            if prev_eth and prev_eth > 0:
                chg = (eth_oi - prev_eth) / prev_eth * 100
                arrow = "↑" if chg > 0 else "↓"
                signals["oi"] = {"text": f"📊 OI: ${oi_b:.2f}B {arrow} ({chg:+.1f}%)", "bias": "neutral"}
            else:
                signals["oi"] = {"text": f"📊 OI: ${oi_b:.2f}B", "bias": "neutral"}
            score["neutral"] += 1
        else:
            signals["oi"] = {"text": "📊 OI: no data", "bias": "neutral"}
    except Exception as e:
        signals["oi"] = {"text": "📊 OI: unavailable", "bias": "neutral"}

    # ── OptionsWatch
    try:
        opt_pain     = modules["optionswatch"].STATE.get("last_max_pain")
        opt_notional = modules["optionswatch"].STATE.get("last_notional")
        opt_expiry   = modules["optionswatch"].STATE.get("last_expiry_str")
        btc_price, _ = _fetch_btc_price()
        if opt_pain and btc_price:
            gap     = btc_price - opt_pain
            gap_pct = gap / btc_price * 100
            dir_str = "above" if gap > 0 else "below"
            b       = "bear" if gap > 0 else "bull"
            signals["options"] = {
                "text": f"⚙️ Max pain: ${opt_pain:,.0f} — BTC ${abs(gap_pct):.1f}% {dir_str} ({opt_expiry})",
                "bias": b
            }
            score[b] += 1
        else:
            signals["options"] = {"text": "⚙️ Options: no data", "bias": "neutral"}
    except Exception as e:
        signals["options"] = {"text": "⚙️ Options: unavailable", "bias": "neutral"}

    return {"signals": signals, "score": score}


# ─── Bias verdict ─────────────────────────────────────────────────────────────

def _bias_verdict(score: dict) -> tuple[str, str]:
    bull = score["bull"]
    bear = score["bear"]
    total = bull + bear

    # Require minimum signal count for any verdict
    if total < 2:
        return "⚪", "INSUFFICIENT DATA"

    ratio = bear / total

    # Strong verdicts require 4+ directional signals
    if total >= 4:
        if ratio >= 0.75:
            return "🔴", "STRONGLY BEARISH"
        elif ratio <= 0.25:
            return "🟢", "STRONGLY BULLISH"

    # Moderate verdicts for 2+ signals
    if ratio >= 0.65:
        return "🟠", "BEARISH"
    elif ratio <= 0.35:
        return "🟡", "BULLISH"
    else:
        return "⚪", "NEUTRAL — Mixed signals"


# ─── Message builder ──────────────────────────────────────────────────────────

def build_intel(modules: dict, is_auto: bool = False) -> str:
    now = datetime.now(timezone.utc)
    btc_price, btc_chg = _fetch_btc_price()

    result  = _evaluate_signals(modules)
    signals = result["signals"]
    score   = result["score"]

    bias_emoji, bias_label = _bias_verdict(score)
    total_signals = score["bull"] + score["bear"] + score["neutral"]
    bias_count    = f"{score['bear']}🔴 {score['bull']}🟢 {score['neutral']}⚪"

    btc_line = ""
    if btc_price:
        chg_emoji = "📈" if (btc_chg or 0) >= 0 else "📉"
        sign      = "+" if (btc_chg or 0) >= 0 else ""
        btc_line  = f"BTC: `${btc_price:,.2f}` {chg_emoji} `{sign}{btc_chg:.2f}%` (4H)"

    trigger_label = "🤖 AUTO-TRIGGER" if is_auto else "on demand"

    lines = [
        f"🧠 *MacroWatch Intel — Full Briefing*",
        f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}  _{trigger_label}_",
        "",
        btc_line,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *MACRO ENVIRONMENT*",
        signals["fedwatch"]["text"],
        signals["trump"]["text"],
        signals["vix"]["text"],
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔬 *MARKET INTERNALS*",
        signals["correl"]["text"],
        signals["funding"]["text"],
        signals["oi"]["text"],
        signals["options"]["text"],
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🤖 *BIAS: {bias_emoji} {bias_label}*",
        f"_{bias_count} — {total_signals} signals evaluated_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Not financial advice. Trade your own plan._ ⚡",
    ]

    return "\n".join([l for l in lines if l is not None])


# ─── Auto-trigger check ───────────────────────────────────────────────────────

def check_auto_trigger(modules: dict) -> bool:
    """
    Returns True and fires public intel if 2+ conditions align.
    Called every 30 minutes from main scheduler.
    """
    if not _auto_cooldown_ok():
        return False

    now        = datetime.now(timezone.utc)
    conditions = []

    # 1. Major macro event within 3h
    try:
        fw_events = modules["fedwatch"].STATE.get("events", [])
        for ev in fw_events:
            start  = ev.get("start")
            impact = ev.get("impact", "").lower()
            if start and impact == "high" and 0 < (start - now).total_seconds() / 3600 <= 3:
                conditions.append("fedwatch_imminent")
                break
    except Exception:
        pass

    # 2. VIX > 25
    try:
        vix = modules["vixwatch"].STATE.get("last_vix")
        if vix and vix >= 25:
            conditions.append("vix_elevated")
    except Exception:
        pass

    # 3. S&R hit in last 30 min
    try:
        hit_times = modules["srwatch"].STATE.get("hit", {})
        for key, t in hit_times.items():
            if (now - t).total_seconds() < 1800:
                conditions.append("sr_hit")
                break
    except Exception:
        pass

    # 4. Extreme funding
    try:
        rates = modules["fundingwatch"].STATE.get("last_rates", {})
        for sym, rate in rates.items():
            if rate is not None and (rate >= 0.10 or rate <= -0.05):
                conditions.append("funding_extreme")
                break
    except Exception:
        pass

    # 5. TrumpWatch score >= 7
    try:
        tw  = modules["trumpwatch"].STATE
        score = tw.get("last_score") or tw.get("last_ai_score")
        if score and score >= 7:
            conditions.append("trump_high")
    except Exception:
        pass

    # 6. BTC moving > 2% in 4H
    try:
        _, btc_chg = _fetch_btc_price()
        if btc_chg is not None and abs(btc_chg) >= 2.0:
            conditions.append("btc_volatile")
    except Exception:
        pass

    # 7. DXY/BTC divergence
    try:
        cw    = modules["correlwatch"].STATE
        dxy   = cw.get("last_dxy")
        btc_c = cw.get("last_btc")
        if dxy and btc_c and ((dxy > 0.4 and btc_c < -2) or (dxy < -0.4 and btc_c > 2)):
            conditions.append("correl_diverge")
    except Exception:
        pass

    if len(conditions) >= 2:
        log.info(f"IntelWatch auto-trigger: {conditions}")
        msg = build_intel(modules, is_auto=True)

        # Fire to public channel
        if PUBLIC_CHAT_ID:
            try:
                import requests as _req
                import os as _os
                _req.post(
                    f"https://api.telegram.org/bot{_os.getenv('TELEGRAM_TOKEN', '')}/sendMessage",
                    json={"chat_id": PUBLIC_CHAT_ID, "text": msg,
                          "parse_mode": "Markdown", "disable_web_page_preview": True},
                    timeout=10,
                )
            except Exception as e:
                log.warning(f"IntelWatch public send failed: {e}")

        # Also fire to private group
        send_text(msg)
        STATE["last_auto_utc"] = now
        STATE["last_bias"]     = conditions
        return True

    return False


# ─── Entry points ─────────────────────────────────────────────────────────────

def show_intel(modules: dict):
    try:
        msg = build_intel(modules, is_auto=False)
        send_text(msg)
        STATE["last_intel_utc"] = datetime.now(timezone.utc)
    except Exception as e:
        log.exception(f"IntelWatch show_intel failed: {e}")
        send_text(f"🧠 [IntelWatch] ⚠️ Error: {str(e)[:200]}")
