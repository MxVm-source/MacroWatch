# bot/modules/weeklybrief.py
"""
MacroWatch Weekly Brief — Rich weekly market recap

Published every Sunday 18:00 UTC.
Also available on demand via /weekly.

Covers:
  - BTC/ETH/market cap weekly performance
  - S&P 500 + Nasdaq weekly change (stooq)
  - Liquidation summary (session data)
  - Funding rate summary
  - Fear & Greed
  - Top crypto performers (CoinGecko trending)
  - Ascent strategy performance (ETH/BNB/SOL)
  - AI-generated narrative (Claude API)

Fires to both private group and public channel.
"""

import logging
import os
import json
from datetime import datetime, timezone, timedelta

import requests

from bot.utils import send_text

log = logging.getLogger("weeklybrief")

PUBLIC_CHAT_ID = os.getenv("PUBLIC_CHAT_ID", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BITGET_BASE    = "https://api.bitget.com"
PRODUCT_TYPE   = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")


# ─── Data fetchers ────────────────────────────────────────────────────────────

def _fetch_crypto_weekly() -> dict:
    """BTC, ETH, total market cap weekly change from CoinGecko."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency":        "usd",
                "ids":                "bitcoin,ethereum",
                "price_change_percentage": "7d",
                "sparkline":          False,
            },
            timeout=10,
        )
        data = r.json()
        result = {}
        for coin in data:
            cid   = coin.get("id")
            price = coin.get("current_price")
            chg7d = coin.get("price_change_percentage_7d_in_currency")
            mcap  = coin.get("market_cap")
            result[cid] = {"price": price, "chg7d": chg7d, "mcap": mcap}
        return result
    except Exception as e:
        log.warning(f"CoinGecko weekly fetch failed: {e}")
        return {}


def _fetch_global_mcap() -> dict:
    """Total crypto market cap + dominance."""
    try:
        r = requests.get(f"{COINGECKO_BASE}/global", timeout=8)
        data = r.json().get("data", {})
        return {
            "total_mcap":    data.get("total_market_cap", {}).get("usd"),
            "mcap_chg_24h":  data.get("market_cap_change_percentage_24h_usd"),
            "btc_dominance": data.get("market_cap_percentage", {}).get("btc"),
        }
    except Exception as e:
        log.warning(f"Global mcap fetch failed: {e}")
        return {}


def _fetch_equity_weekly() -> dict:
    """S&P 500 and Nasdaq weekly change from stooq (free, no key)."""
    result = {}
    symbols = {"sp500": "^spx", "nasdaq": "^ndx"}
    for name, sym in symbols.items():
        try:
            r = requests.get(
                f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
                timeout=8,
            )
            lines = r.text.strip().splitlines()
            if len(lines) >= 2:
                parts  = lines[1].split(",")
                close  = float(parts[6])
                open_  = float(parts[3])
                if open_ > 0:
                    result[name] = round((close - open_) / open_ * 100, 2)
        except Exception as e:
            log.warning(f"Stooq {name} fetch failed: {e}")
    return result


def _fetch_fear_greed() -> dict:
    """Fear & Greed index."""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=6,
        )
        d = r.json().get("data", [{}])[0]
        return {"value": int(d.get("value", 0)), "label": d.get("value_classification", "")}
    except Exception as e:
        log.warning(f"Fear & Greed fetch failed: {e}")
        return {}


def _fetch_trending() -> list:
    """Top 5 trending coins from CoinGecko."""
    try:
        r = requests.get(f"{COINGECKO_BASE}/search/trending", timeout=8)
        coins = r.json().get("coins", [])[:5]
        return [c["item"]["symbol"].upper() for c in coins]
    except Exception as e:
        log.warning(f"Trending fetch failed: {e}")
        return []


def _fetch_asset_weekly(symbol: str) -> float | None:
    """Fetch 7-day price change for a Bitget perp symbol."""
    try:
        r = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": "1D",
                    "limit": "8", "productType": PRODUCT_TYPE},
            timeout=8,
        )
        data = r.json()
        if data.get("code") != "00000":
            return None
        candles = data.get("data") or []
        if len(candles) < 7:
            return None
        open_price  = float(candles[-7][1])
        close_price = float(candles[-1][4])
        if open_price > 0:
            return round((close_price - open_price) / open_price * 100, 2)
    except Exception as e:
        log.warning(f"Asset weekly fetch failed for {symbol}: {e}")
    return None


def _fetch_liq_summary(modules: dict) -> dict:
    """Pull liquidation stats from LiquidationWatch state."""
    try:
        stats      = modules["liquidationwatch"].STATE.get("stats", {})
        total_long = sum(v.get("long_liqs", 0) for v in stats.values())
        total_short= sum(v.get("short_liqs", 0) for v in stats.values())
        total_usd  = sum(v.get("total_usd", 0) for v in stats.values())
        return {"long": total_long, "short": total_short, "usd_m": total_usd / 1e6}
    except Exception:
        return {}


def _fetch_funding_summary(modules: dict) -> dict:
    """Pull funding rates from FundingWatch state."""
    try:
        rates = modules["fundingwatch"].STATE.get("last_rates", {})
        return {k.replace("USDT", ""): v for k, v in rates.items() if v is not None}
    except Exception:
        return {}


# ─── AI narrative ────────────────────────────────────────────────────────────

def _generate_narrative(data: dict) -> str:
    """Use Claude API to generate a 3-line market narrative."""
    try:
        prompt = f"""You are a crypto market analyst writing a weekly brief for traders.
Based on this week's data, write exactly 3 short punchy sentences explaining:
1. The dominant market theme this week
2. What drove price action
3. What to watch next week

Data:
- BTC: {data.get('btc_chg', 'N/A')}% this week, price ${data.get('btc_price', 'N/A')}
- ETH: {data.get('eth_chg', 'N/A')}% this week
- Total market cap: ${data.get('mcap_t', 'N/A')}T
- S&P 500: {data.get('sp500', 'N/A')}% | Nasdaq: {data.get('nasdaq', 'N/A')}%
- Fear & Greed: {data.get('fg_value', 'N/A')} ({data.get('fg_label', 'N/A')})
- BTC dominance: {data.get('btc_dom', 'N/A')}%
- Liq dominant: {'SHORTS' if (data.get('short_liqs', 0) > data.get('long_liqs', 0)) else 'LONGS'}

Be direct, no fluff. Max 40 words total. No emojis."""

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 120,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        content = response.json().get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block["text"].strip()
    except Exception as e:
        log.warning(f"AI narrative failed: {e}")
    return ""


# ─── Message builder ──────────────────────────────────────────────────────────

def build_weekly_brief(modules: dict) -> str:
    now        = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end   = now.strftime("%b %d, %Y")

    # Fetch all data
    crypto   = _fetch_crypto_weekly()
    global_  = _fetch_global_mcap()
    equities = _fetch_equity_weekly()
    fg       = _fetch_fear_greed()
    trending = _fetch_trending()
    liqs     = _fetch_liq_summary(modules)
    funding  = _fetch_funding_summary(modules)

    btc = crypto.get("bitcoin", {})
    eth = crypto.get("ethereum", {})

    # Ascent strategy assets
    eth_w = _fetch_asset_weekly("ETHUSDT")
    bnb_w = _fetch_asset_weekly("BNBUSDT")
    sol_w = _fetch_asset_weekly("SOLUSDT")

    # Market cap in trillions
    mcap_t = round(global_.get("total_mcap", 0) / 1e12, 2) if global_.get("total_mcap") else None

    # AI narrative data bundle
    narrative_data = {
        "btc_chg":    round(btc.get("chg7d", 0), 2) if btc.get("chg7d") else "N/A",
        "btc_price":  f"{btc.get('price', 0):,.0f}" if btc.get("price") else "N/A",
        "eth_chg":    round(eth.get("chg7d", 0), 2) if eth.get("chg7d") else "N/A",
        "mcap_t":     mcap_t or "N/A",
        "sp500":      equities.get("sp500", "N/A"),
        "nasdaq":     equities.get("nasdaq", "N/A"),
        "fg_value":   fg.get("value", "N/A"),
        "fg_label":   fg.get("label", "N/A"),
        "btc_dom":    round(global_.get("btc_dominance", 0), 1) if global_.get("btc_dominance") else "N/A",
        "long_liqs":  liqs.get("long", 0),
        "short_liqs": liqs.get("short", 0),
    }
    narrative = _generate_narrative(narrative_data)

    # ── Build message ─────────────────────────────────────────────────────────
    def _pct(val, plus=True):
        if val is None:
            return "N/A"
        sign = "+" if val >= 0 and plus else ""
        return f"{sign}{val:.2f}%"

    def _chg_emoji(val):
        if val is None:
            return "➡️"
        return "📈" if val >= 0 else "📉"

    lines = [
        f"📊 *MacroWatch Weekly Brief*",
        f"📅 {week_start} → {week_end}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *MARKET OVERVIEW*",
        "",
    ]

    # BTC
    if btc.get("price"):
        lines.append(
            f"₿ *BTC*  `${btc['price']:,.0f}`  "
            f"{_chg_emoji(btc.get('chg7d'))} `{_pct(btc.get('chg7d'))}`"
        )
    # ETH
    if eth.get("price"):
        lines.append(
            f"Ξ *ETH*  `${eth['price']:,.2f}`  "
            f"{_chg_emoji(eth.get('chg7d'))} `{_pct(eth.get('chg7d'))}`"
        )
    # Market cap
    if mcap_t:
        lines.append(f"🌐 *Market Cap*  `${mcap_t}T`")
    # BTC dominance
    if global_.get("btc_dominance"):
        lines.append(f"👑 *BTC Dom*  `{global_['btc_dominance']:.1f}%`")

    lines.append("")

    # Equities
    if equities:
        lines.append("📈 *Equities*")
        if equities.get("sp500") is not None:
            e = "📈" if equities["sp500"] >= 0 else "📉"
            lines.append(f"  S&P 500: {e} `{_pct(equities['sp500'])}`")
        if equities.get("nasdaq") is not None:
            e = "📈" if equities["nasdaq"] >= 0 else "📉"
            lines.append(f"  Nasdaq:  {e} `{_pct(equities['nasdaq'])}`")
        lines.append("")

    # Fear & Greed
    if fg.get("value"):
        val = fg["value"]
        if val >= 75:   fg_emoji = "🟢"
        elif val >= 55: fg_emoji = "🟡"
        elif val >= 45: fg_emoji = "🟠"
        else:           fg_emoji = "🔴"
        lines.append(f"🎭 *Fear & Greed*  `{val}` — {fg_emoji} {fg['label']}")
        lines.append("")

    # AI Narrative
    if narrative:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "🧠 *MARKET NARRATIVE*",
            "",
            f"_{narrative}_",
            "",
        ]

    # Liquidations
    if liqs.get("usd_m", 0) > 0:
        dom  = "SHORT liqs dominated 🔴" if liqs.get("short", 0) > liqs.get("long", 0) else "LONG liqs dominated 🟢"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "💥 *LIQUIDATIONS*",
            "",
            f"{dom}",
            f"Total: `${liqs['usd_m']:.1f}M`  "
            f"({liqs.get('long', 0)} long / {liqs.get('short', 0)} short events)",
            "",
        ]

    # Funding
    if funding:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "💸 *FUNDING RATES*",
            "",
        ]
        for asset, rate in funding.items():
            if rate is not None:
                e = "🔴" if rate > 0.05 else "🟢" if rate < -0.03 else "⚪"
                lines.append(f"  {e} {asset}: `{rate:+.4f}%`")
        lines.append("")

    # Trending
    if trending:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "🚀 *TRENDING THIS WEEK*",
            "",
            "  " + " · ".join(f"*{t}*" for t in trending[:5]),
            "",
        ]

    # Ascent strategy
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🤖 *ASCENT STRATEGY*",
        "",
    ]
    for label, chg in [("ETH", eth_w), ("BNB", bnb_w), ("SOL", sol_w)]:
        if chg is not None:
            e    = "📈" if chg >= 0 else "📉"
            sign = "+" if chg >= 0 else ""
            lines.append(f"  {e} {label}:  `{sign}{chg:.2f}%` this week")
        else:
            lines.append(f"  {label}: N/A")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"_Published {now.strftime('%A %d %B %Y, %H:%M UTC')}_",
        "_Not financial advice. Trade your own plan._ ⚡",
    ]

    return "\n".join(lines)


# ─── Entry point ─────────────────────────────────────────────────────────────

def send_weekly_brief(modules: dict):
    try:
        msg = build_weekly_brief(modules)
    except Exception as e:
        log.exception(f"WeeklyBrief build failed: {e}")
        send_text(f"📊 [WeeklyBrief] ⚠️ Build failed: {str(e)[:200]}")
        return

    # Send to private group
    send_text(msg)

    # Send to public channel
    if PUBLIC_CHAT_ID:
        try:
            import os as _os
            requests.post(
                f"https://api.telegram.org/bot{_os.getenv('TELEGRAM_TOKEN', '')}/sendMessage",
                json={"chat_id": PUBLIC_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10,
            )
            log.info("WeeklyBrief sent to public channel ✅")
        except Exception as e:
            log.warning(f"WeeklyBrief public send failed: {e}")

    log.info("WeeklyBrief sent ✅")
