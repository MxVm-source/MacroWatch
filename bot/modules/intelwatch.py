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
    # V2: history tracking for "vs 4h ago" comparison
    "history":           [],   # list of {"utc": dt, "net": float, "bias": str}
    "last_regime":       None,
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


def _fetch_eth_price() -> float | None:
    """Returns current ETH price."""
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/ticker",
            params={"symbol": "ETHUSDT", "productType": PRODUCT_TYPE},
            timeout=6,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None
        d = data.get("data") or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        price = float(d.get("lastPr") or d.get("last") or 0)
        return round(price, 2) if price > 0 else None
    except Exception as e:
        log.warning(f"ETH price fetch failed: {e}")
        return None


# ─── Signal evaluator ─────────────────────────────────────────────────────────

def _evaluate_signals(modules: dict) -> dict:
    """
    V2 weighted scoring. Each signal contributes positive (bull) or negative
    (bear) points, scaled by importance. Returns:
      - signals: dict of {key: {text, bias, points}} for display
      - net: float — sum of signed points (negative=bearish, positive=bullish)
      - bull_count, bear_count, neutral_count: counts for display
    """
    now    = datetime.now(timezone.utc)
    signals = {}
    net    = 0.0
    bull_count = bear_count = neutral_count = 0

    def _record(key: str, text: str, points: float, label: str):
        """Helper to record a signal with its weighted points."""
        nonlocal net, bull_count, bear_count, neutral_count
        signals[key] = {"text": text, "points": points, "label": label}
        net += points
        if points > 0:    bull_count += 1
        elif points < 0:  bear_count += 1
        else:             neutral_count += 1

    # ── FedWatch — weight: ±2 if event <3h, otherwise just info
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
            impact  = (next_event.get("impact") or "medium").lower()
            label   = next_event.get("title", "Event")[:40]
            if delta_h <= 3 and impact == "high":
                # Imminent high-impact event — neutral but counts as caution
                # Bias is neutral but flagged via 0 points (informational)
                _record("fedwatch", f"⚠️ {label} in {int(delta_h*60)}m — HIGH IMPACT", 0, "Neutral")
            elif delta_h <= 24:
                _record("fedwatch", f"📅 {label} in {int(delta_h)}h", 0, "Neutral")
            else:
                _record("fedwatch", f"📅 Next: {label}", 0, "Neutral")
        else:
            _record("fedwatch", "📅 No major events upcoming", 0, "Neutral")
    except Exception:
        _record("fedwatch", "📅 FedWatch: unavailable", 0, "—")

    # ── TrumpWatch — weight: ±3 if score≥9, ±2 if 7-8
    try:
        tw_state   = modules["trumpwatch"].STATE
        last_score = tw_state.get("last_score") or tw_state.get("last_ai_score")
        last_bias  = tw_state.get("last_bias") or tw_state.get("last_sentiment", "")
        last_alert = tw_state.get("last_alert_utc")

        # Only count if we had a Trump alert in the last 4 hours
        recent = last_alert and (now - last_alert) < timedelta(hours=4)

        if last_score is not None and recent:
            bear = "bearish" in str(last_bias).lower()
            bull = "bullish" in str(last_bias).lower()
            emoji = "🔴" if bear else "🟢" if bull else "🟡"
            # Weighted by score intensity
            magnitude = 3 if last_score >= 9 else 2 if last_score >= 7 else 0
            pts = -magnitude if bear else magnitude if bull else 0
            label = "Bearish" if bear else "Bullish" if bull else "Neutral"
            _record("trump", f"🍊 Score {last_score}/10 {emoji} {label} ({pts:+.0f})", pts, label)
        else:
            _record("trump", "🍊 TrumpWatch: no recent alerts", 0, "—")
    except Exception:
        _record("trump", "🍊 TrumpWatch: unavailable", 0, "—")

    # ── VixWatch — weight: ±2 (regime indicator)
    try:
        vw_state = modules["vixwatch"].STATE
        vix      = vw_state.get("last_vix")
        if vix:
            if vix >= 30:
                pts, emoji, regime = -2, "🔴", "HIGH FEAR"
            elif vix >= 25:
                pts, emoji, regime = -2, "🟠", "ELEVATED"
            elif vix >= 20:
                pts, emoji, regime = -1, "🟡", "MILD CAUTION"
            elif vix < 15:
                pts, emoji, regime = +2, "🟢", "VERY CALM"
            else:
                pts, emoji, regime = +1, "🟢", "CALM"
            label = "Bearish" if pts < 0 else "Bullish" if pts > 0 else "Neutral"
            _record("vix", f"😱 VIX: {vix:.1f} {emoji} {regime} ({pts:+.0f})", pts, label)
        else:
            _record("vix", "😱 VIX: unavailable", 0, "—")
    except Exception:
        _record("vix", "😱 VIX: unavailable", 0, "—")

    # ── BTC 4H movement — weight: ±2 if ≥2% in 4H
    try:
        btc_price, btc_chg = _fetch_btc_price()
        if btc_chg is not None:
            if btc_chg >= 2.0:
                _record("btc_move", f"📈 BTC 4H: +{btc_chg:.2f}% — strong move (+2)", 2, "Bullish")
            elif btc_chg <= -2.0:
                _record("btc_move", f"📉 BTC 4H: {btc_chg:.2f}% — strong move (-2)", -2, "Bearish")
            else:
                _record("btc_move", f"📊 BTC 4H: {btc_chg:+.2f}% — quiet", 0, "Neutral")
    except Exception:
        pass

    # ── CorrelWatch — weight: ±1 (DXY/BTC divergence)
    try:
        cw_state = modules["correlwatch"].STATE
        dxy = cw_state.get("last_dxy")
        btc = cw_state.get("last_btc")
        if dxy is not None and btc is not None:
            if dxy > 0 and btc < 0:
                _record("correl", f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% 🔴 Bearish (-1)", -1, "Bearish")
            elif dxy < 0 and btc > 0:
                _record("correl", f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% 🟢 Bullish (+1)", 1, "Bullish")
            else:
                _record("correl", f"📡 DXY {dxy:+.2f}% / BTC {btc:+.2f}% ⚪ Aligned", 0, "Neutral")
        else:
            _record("correl", "📡 CorrelWatch: no data", 0, "—")
    except Exception:
        _record("correl", "📡 CorrelWatch: unavailable", 0, "—")

    # ── FundingWatch — weight: ±1 (extreme = squeeze risk)
    try:
        fw_rates = modules["fundingwatch"].STATE.get("last_rates", {})
        btc_rate = fw_rates.get("BTCUSDT")
        # Use BTC funding if available, otherwise ETH as proxy
        rate = btc_rate if btc_rate is not None else fw_rates.get("ETHUSDT")
        if rate is not None:
            if rate >= 0.10:
                _record("funding", f"💸 Funding: {rate:+.4f}% 🔴 Overleveraged longs (-1)", -1, "Bearish")
            elif rate <= -0.05:
                _record("funding", f"💸 Funding: {rate:+.4f}% 🔵 Overleveraged shorts (+1)", 1, "Bullish")
            else:
                _record("funding", f"💸 Funding: {rate:+.4f}% ⚪ Neutral", 0, "Neutral")
        else:
            _record("funding", "💸 Funding: no data", 0, "—")
    except Exception:
        _record("funding", "💸 Funding: unavailable", 0, "—")

    # ── OIWatch — weight: ±1 if spike + price direction confirms
    try:
        oi_data  = modules["oiwatch"].STATE.get("last_oi", {})
        prev_oi  = modules["oiwatch"].STATE.get("prev_oi", {})
        oi_btc   = oi_data.get("BTCUSDT")
        prev_btc = prev_oi.get("BTCUSDT")

        # Need price direction to interpret OI move
        _, btc_chg = _fetch_btc_price()
        if oi_btc and prev_btc and prev_btc > 0:
            chg_pct = (oi_btc - prev_btc) / prev_btc * 100
            arrow   = "↑" if chg_pct > 0 else "↓"
            oi_b    = oi_btc / 1e9
            # OI ↑ + price ↑ = longs adding (bullish)
            # OI ↑ + price ↓ = shorts piling in (bearish)
            # OI ↓ + price ↑ = shorts covering (mildly bullish)
            # OI ↓ + price ↓ = longs capitulating (mildly bullish reversal)
            if abs(chg_pct) >= 5 and btc_chg is not None:
                if chg_pct > 0 and btc_chg > 0:
                    _record("oi", f"📊 OI: ${oi_b:.2f}B {arrow} ({chg_pct:+.1f}%) 🟢 Longs adding (+1)", 1, "Bullish")
                elif chg_pct > 0 and btc_chg < 0:
                    _record("oi", f"📊 OI: ${oi_b:.2f}B {arrow} ({chg_pct:+.1f}%) 🔴 Shorts piling in (-1)", -1, "Bearish")
                else:
                    _record("oi", f"📊 OI: ${oi_b:.2f}B {arrow} ({chg_pct:+.1f}%) ⚪ Position unwind", 0, "Neutral")
            else:
                _record("oi", f"📊 OI: ${oi_b:.2f}B {arrow} ({chg_pct:+.1f}%) ⚪ Stable", 0, "Neutral")
        elif oi_btc:
            _record("oi", f"📊 OI: ${oi_btc/1e9:.2f}B ⚪ Baseline", 0, "Neutral")
        else:
            _record("oi", "📊 OI: no data", 0, "—")
    except Exception:
        _record("oi", "📊 OI: unavailable", 0, "—")

    # ── OptionsWatch — BTC max pain (±0.5)
    try:
        btc_state = modules["optionswatch"].STATE.get("btc", {})
        btc_pain  = btc_state.get("max_pain")
        btc_expiry = btc_state.get("expiry_str")
        btc_price, _ = _fetch_btc_price()
        if btc_pain and btc_price:
            gap_pct = (btc_price - btc_pain) / btc_price * 100
            dir_str = "above" if gap_pct > 0 else "below"
            pts = -0.5 if gap_pct > 0 else 0.5
            label = "Bearish" if pts < 0 else "Bullish"
            _record("options_btc",
                    f"   BTC: ${btc_pain/1000:.0f}k — {abs(gap_pct):.1f}% {dir_str} ({pts:+.1f})",
                    pts, label)
        else:
            _record("options_btc", "   BTC: no data", 0, "—")
    except Exception:
        _record("options_btc", "   BTC: unavailable", 0, "—")

    # ── OptionsWatch — ETH max pain (±0.5)
    try:
        eth_state = modules["optionswatch"].STATE.get("eth", {})
        eth_pain  = eth_state.get("max_pain")
        eth_expiry = eth_state.get("expiry_str")
        eth_price = _fetch_eth_price()
        if eth_pain and eth_price:
            gap_pct = (eth_price - eth_pain) / eth_price * 100
            dir_str = "above" if gap_pct > 0 else "below"
            pts = -0.5 if gap_pct > 0 else 0.5
            label = "Bearish" if pts < 0 else "Bullish"
            _record("options_eth",
                    f"   ETH: ${eth_pain:,.0f} — {abs(gap_pct):.1f}% {dir_str} ({pts:+.1f})",
                    pts, label)
        else:
            _record("options_eth", "   ETH: no data", 0, "—")
    except Exception:
        _record("options_eth", "   ETH: unavailable", 0, "—")

    return {
        "signals":       signals,
        "net":           net,
        "bull_count":    bull_count,
        "bear_count":    bear_count,
        "neutral_count": neutral_count,
    }


# ─── Regime detection ─────────────────────────────────────────────────────────

def _detect_regime(modules: dict) -> str:
    """
    Detect BTC market regime from daily structure.
    Returns one of: TRENDING UP, TRENDING DOWN, CHOP, VOLATILE.
    """
    try:
        # Fetch 60 daily candles for 50EMA + recent action
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/candles",
            params={"symbol": "BTCUSDT", "granularity": "1D",
                    "limit": "60", "productType": PRODUCT_TYPE},
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return "—"
        candles = data.get("data") or []
        if len(candles) < 50:
            return "—"

        closes = [float(c[4]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        price  = closes[-1]

        # Compute 50-day EMA
        k = 2 / 51
        ema = sum(closes[:50]) / 50
        for v in closes[50:]:
            ema = v * k + ema * (1 - k)

        # EMA slope (last 5 days vs prior 5)
        recent_avg = sum(closes[-5:]) / 5
        prior_avg  = sum(closes[-10:-5]) / 5
        ema_rising = recent_avg > prior_avg

        # Daily range last 7 days
        recent_high = max(highs[-7:])
        recent_low  = min(lows[-7:])
        range_pct   = (recent_high - recent_low) / recent_low * 100

        # VIX check for VOLATILE override
        try:
            vix = modules["vixwatch"].STATE.get("last_vix")
        except Exception:
            vix = None

        # Decide regime
        # VOLATILE wins if range > 8% in week or VIX > 22
        if range_pct > 8 or (vix and vix > 22):
            return "VOLATILE"

        gap_to_ema = (price - ema) / ema * 100
        if gap_to_ema > 3 and ema_rising:
            return "TRENDING UP"
        elif gap_to_ema < -3 and not ema_rising:
            return "TRENDING DOWN"
        else:
            return "CHOP"
    except Exception as e:
        log.warning(f"Regime detection failed: {e}")
        return "—"


# ─── History tracking for "vs prior" ──────────────────────────────────────────

def _record_history(net: float, bias: str):
    """Add current bias snapshot to history (max 24 entries)."""
    STATE["history"].append({
        "utc":  datetime.now(timezone.utc),
        "net":  net,
        "bias": bias,
    })
    # Keep last 24 hours of snapshots (cap at 100 entries)
    if len(STATE["history"]) > 100:
        STATE["history"] = STATE["history"][-100:]


def _compare_vs_prior(current_net: float, current_bias: str, hours: int = 4) -> str:
    """Find the most recent history entry from ~hours ago and compare."""
    history = STATE.get("history", [])
    if not history:
        return ""

    now    = datetime.now(timezone.utc)
    target = now - timedelta(hours=hours)
    # Find closest entry to target
    candidates = [h for h in history if h["utc"] <= target + timedelta(minutes=30)]
    if not candidates:
        return ""
    prior = min(candidates, key=lambda h: abs((h["utc"] - target).total_seconds()))
    prior_net  = prior["net"]
    prior_bias = prior["bias"]

    # Same direction (both bull or both bear)
    same_dir = (
        (current_net > 0 and prior_net > 0) or
        (current_net < 0 and prior_net < 0) or
        (abs(current_net) <= 1 and abs(prior_net) <= 1)
    )

    if not same_dir and abs(prior_net) > 1 and abs(current_net) > 1:
        return f"🔄 vs 4h ago: flipped from {prior_bias} ({prior_net:+.1f} → {current_net:+.1f})"

    diff = current_net - prior_net
    if abs(diff) < 1:
        return f"🔄 vs 4h ago: stable at {prior_bias}"
    if (current_net > 0 and diff > 0) or (current_net < 0 and diff < 0):
        return f"🔄 vs 4h ago: strengthening from {prior_bias} ({prior_net:+.1f} → {current_net:+.1f})"
    else:
        return f"🔄 vs 4h ago: weakening from {prior_bias} ({prior_net:+.1f} → {current_net:+.1f})"


# ─── Bias verdict ─────────────────────────────────────────────────────────────

def _bias_verdict(net: float) -> tuple[str, str]:
    """V2 weighted bias from net points (sum of all signal weights)."""
    if net >= 5:
        return "🟢", "STRONGLY BULLISH"
    elif net >= 2:
        return "🟢", "BULLISH"
    elif net <= -5:
        return "🔴", "STRONGLY BEARISH"
    elif net <= -2:
        return "🔴", "BEARISH"
    else:
        return "⚪", "NEUTRAL"


# ─── Message builder ──────────────────────────────────────────────────────────

def build_intel(modules: dict, is_auto: bool = False, custom_header: str | None = None, custom_label: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    btc_price, btc_chg = _fetch_btc_price()

    result        = _evaluate_signals(modules)
    signals       = result["signals"]
    net           = result["net"]
    bull_count    = result["bull_count"]
    bear_count    = result["bear_count"]
    neutral_count = result["neutral_count"]
    total_signals = bull_count + bear_count + neutral_count

    bias_emoji, bias_label = _bias_verdict(net)

    # Detect regime
    regime = _detect_regime(modules)
    regime_emoji = {
        "TRENDING UP":   "🚀",
        "TRENDING DOWN": "📉",
        "CHOP":          "↔️",
        "VOLATILE":      "🌪️",
    }.get(regime, "⚪")

    # Compare vs 4h ago
    vs_prior = _compare_vs_prior(net, bias_label, hours=4)

    # Record this snapshot to history
    _record_history(net, bias_label)

    btc_line = ""
    if btc_price:
        chg_emoji = "📈" if (btc_chg or 0) >= 0 else "📉"
        sign      = "+" if (btc_chg or 0) >= 0 else ""
        btc_line  = f"BTC: `${btc_price:,.2f}` {chg_emoji} `{sign}{btc_chg:.2f}%` (4H)"

    # Header variants
    if custom_header:
        header_line1 = custom_header
        header_line2 = custom_label or now.strftime('%A %d %B %Y · %H:%M UTC')
    else:
        trigger_label = "🤖 AUTO-TRIGGER" if is_auto else "on demand"
        header_line1  = "🧠 *MacroWatch Intel — Full Briefing*"
        header_line2  = f"🕐 {now.strftime('%Y-%m-%d %H:%M UTC')}  _{trigger_label}_"

    lines = [
        header_line1,
        header_line2,
        "",
        btc_line,
        f"{regime_emoji} *Regime: {regime}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *MACRO ENVIRONMENT*",
        signals.get("fedwatch", {}).get("text", ""),
        signals.get("trump",    {}).get("text", ""),
        signals.get("vix",      {}).get("text", ""),
    ]

    # BTC move line (only show if recorded)
    if "btc_move" in signals:
        lines.append(signals["btc_move"]["text"])

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔬 *MARKET INTERNALS*",
        signals.get("correl",  {}).get("text", ""),
        signals.get("funding", {}).get("text", ""),
        signals.get("oi",      {}).get("text", ""),
        "⚙️ *Max pain*",
        signals.get("options_btc", {}).get("text", ""),
        signals.get("options_eth", {}).get("text", ""),
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🤖 *BIAS: {bias_emoji} {bias_label}*  (Net: `{net:+.1f}`)",
        f"_📊 Signals: {bear_count}🔴  {bull_count}🟢  {neutral_count}⚪_",
    ]

    if vs_prior:
        lines.append(f"_{vs_prior}_")

    return "\n".join([l for l in lines if l is not None])


# ─── Auto-trigger check ───────────────────────────────────────────────────────

def check_auto_trigger(modules: dict) -> bool:
    """
    V2 auto-trigger. Fires when ANY of these is true:
      1. Weighted score crosses |±3| (bias became BULLISH or BEARISH)
      2. A single high-impact signal fires (Trump score ≥ 9, macro event < 30min away)
      3. Regime flip detected (e.g. CHOP → TRENDING UP)

    Cooldown: 2h. Fires to PRIVATE group only.
    """
    if not _auto_cooldown_ok():
        return False

    now     = datetime.now(timezone.utc)
    reasons = []

    # Evaluate signals once (also used for net score)
    try:
        result = _evaluate_signals(modules)
        net    = result["net"]
    except Exception as e:
        log.warning(f"Auto-trigger eval failed: {e}")
        return False

    # ── Trigger 1: Net weighted score crosses |±3|
    if abs(net) >= 3:
        reasons.append(f"weighted_score (net {net:+.1f})")

    # ── Trigger 2a: TrumpWatch ≥ 9 in last hour (very high impact)
    try:
        tw         = modules["trumpwatch"].STATE
        last_score = tw.get("last_score") or tw.get("last_ai_score")
        last_alert = tw.get("last_alert_utc")
        if last_score and last_score >= 9 and last_alert:
            if (now - last_alert) < timedelta(hours=1):
                reasons.append(f"trump_critical ({last_score}/10)")
    except Exception:
        pass

    # ── Trigger 2b: High-impact macro event within 30 min
    try:
        fw_events = modules["fedwatch"].STATE.get("events", [])
        for ev in fw_events:
            start  = ev.get("start")
            impact = (ev.get("impact") or "").lower()
            if start and impact == "high":
                delta_min = (start - now).total_seconds() / 60
                if 0 < delta_min <= 30:
                    reasons.append(f"macro_imminent ({ev.get('title', 'event')})")
                    break
    except Exception:
        pass

    # ── Trigger 3: Regime flip detected
    try:
        current_regime = _detect_regime(modules)
        last_regime    = STATE.get("last_regime")
        if last_regime and current_regime != "—" and current_regime != last_regime:
            reasons.append(f"regime_flip ({last_regime} → {current_regime})")
        # Update regime memory
        if current_regime != "—":
            STATE["last_regime"] = current_regime
    except Exception:
        pass

    # Fire if any reason triggered
    if reasons:
        log.info(f"IntelWatch auto-trigger: {reasons}")
        msg = build_intel(modules, is_auto=True)

        # Auto-trigger fires to PRIVATE group only (public gets scheduled Wed Deep Dive)
        send_text(msg)
        STATE["last_auto_utc"] = now
        STATE["last_bias"]     = reasons
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


def send_weekly_intel(modules: dict):
    """
    Wednesday 09:00 UTC scheduled Intel Deep Dive.
    Fires to BOTH private group and public channel.
    """
    import os as _os
    import requests as _req

    now = datetime.now(timezone.utc)
    PUBLIC_CHAT_ID = _os.getenv("PUBLIC_CHAT_ID", "")
    TG_TOKEN       = _os.getenv("TELEGRAM_TOKEN", "")

    try:
        msg = build_intel(
            modules,
            is_auto=False,
            custom_header="🧠 *MacroWatch Intel — Weekly Deep Dive*",
            custom_label=f"📅 {now.strftime('%A, %b %d, %Y')} · {now.strftime('%H:%M UTC')}",
        )
    except Exception as e:
        log.exception(f"WeeklyIntel build failed: {e}")
        send_text(f"🧠 [IntelWatch] ⚠️ Weekly build failed: {str(e)[:200]}")
        return

    # Send text to private
    send_text(msg)

    # Send text to public
    if PUBLIC_CHAT_ID and TG_TOKEN:
        try:
            _req.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": PUBLIC_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"WeeklyIntel public send failed: {e}")

    STATE["last_intel_utc"] = now
    log.info("WeeklyIntel (Wed) sent ✅")
