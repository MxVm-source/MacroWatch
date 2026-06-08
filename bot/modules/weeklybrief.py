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
  - ATRb v2 strategy performance (ETH)
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
    """BTC, ETH, total market cap weekly change from CoinGecko.
       Falls back to Bitget for prices if CoinGecko fails/rate-limited."""
    result = {}
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency":             "usd",
                "ids":                     "bitcoin,ethereum",
                "price_change_percentage": "7d",
                "sparkline":               False,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"CoinGecko HTTP {r.status_code}: {r.text[:200]}")
        else:
            data = r.json()
            # Validate response is a list (CoinGecko returns dict on rate-limit error)
            if isinstance(data, list):
                for coin in data:
                    cid   = coin.get("id")
                    price = coin.get("current_price")
                    chg7d = coin.get("price_change_percentage_7d_in_currency")
                    mcap  = coin.get("market_cap")
                    if cid:
                        result[cid] = {"price": price, "chg7d": chg7d, "mcap": mcap}
            else:
                log.warning(f"CoinGecko returned non-list response: {str(data)[:200]}")
    except Exception as e:
        log.warning(f"CoinGecko weekly fetch failed: {e}")

    # Fallback: fetch BTC/ETH from Bitget if CoinGecko didn't deliver
    if "bitcoin" not in result or not result.get("bitcoin", {}).get("price"):
        log.info("CoinGecko BTC missing — falling back to Bitget")
        btc_data = _fetch_bitget_weekly("BTCUSDT")
        if btc_data:
            result["bitcoin"] = btc_data
    if "ethereum" not in result or not result.get("ethereum", {}).get("price"):
        log.info("CoinGecko ETH missing — falling back to Bitget")
        eth_data = _fetch_bitget_weekly("ETHUSDT")
        if eth_data:
            result["ethereum"] = eth_data

    return result


def _fetch_bitget_weekly(symbol: str) -> dict | None:
    """Fallback price + 7d change fetcher using Bitget candles."""
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
        if len(candles) < 2:
            return None
        price      = float(candles[-1][4])
        prev_close = float(candles[0][4])
        chg7d      = (price - prev_close) / prev_close * 100 if prev_close > 0 else None
        return {"price": price, "chg7d": chg7d, "mcap": None}
    except Exception as e:
        log.warning(f"Bitget fallback fetch failed for {symbol}: {e}")
        return None


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


def _fetch_yahoo_weekly(yahoo_symbol: str) -> float | None:
    """Fetch 7-day % change from Yahoo Finance — more reliable than stooq."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
            params={"interval": "1d", "range": "1mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code != 200:
            log.warning(f"Yahoo HTTP {r.status_code} for {yahoo_symbol}")
            return None
        data    = r.json()
        result  = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        valid  = [c for c in closes if c is not None and c > 0]
        if len(valid) < 8:
            log.warning(f"Yahoo {yahoo_symbol}: only {len(valid)} valid closes")
            return None
        recent      = valid[-8:]
        open_price  = recent[0]
        close_price = recent[-1]
        if open_price > 0:
            return round((close_price - open_price) / open_price * 100, 2)
    except Exception as e:
        log.warning(f"Yahoo {yahoo_symbol} weekly fetch failed: {e}")
    return None


def _fetch_stooq_weekly(symbol: str) -> float | None:
    """Fallback: stooq. Frequently returns JS challenge pages; we detect that."""
    try:
        r = requests.get(
            f"https://stooq.com/q/d/l/?s={symbol}&i=d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code != 200:
            log.warning(f"Stooq HTTP {r.status_code} for {symbol}")
            return None
        # Detect anti-bot HTML page (starts with HTML / JS, not CSV header)
        if not r.text.strip().lower().startswith("date,"):
            log.warning(f"Stooq {symbol}: blocked (non-CSV response)")
            return None
        csv_lines = r.text.strip().splitlines()
        if len(csv_lines) < 9:
            return None
        rows = csv_lines[1:]
        valid = []
        for row in rows:
            cols = row.split(",")
            if len(cols) < 5:
                continue
            try:
                close = float(cols[4])
                if close > 0:
                    valid.append(close)
            except (ValueError, IndexError):
                continue
        if len(valid) < 8:
            return None
        recent      = valid[-8:]
        open_price  = recent[0]
        close_price = recent[-1]
        if open_price > 0:
            return round((close_price - open_price) / open_price * 100, 2)
    except Exception as e:
        log.warning(f"Stooq {symbol} weekly fetch failed: {e}")
    return None


def _fetch_with_fallback(yahoo_sym: str, stooq_sym: str) -> float | None:
    """Try Yahoo first, then stooq as backup."""
    val = _fetch_yahoo_weekly(yahoo_sym)
    if val is not None:
        return val
    return _fetch_stooq_weekly(stooq_sym)


def _fetch_equity_weekly() -> dict:
    """S&P 500, Nasdaq, Apple 7-day change — Yahoo primary, stooq fallback."""
    return {
        "sp500":  _fetch_with_fallback("^GSPC",  "^spx"),
        "nasdaq": _fetch_with_fallback("^NDX",   "^ndx"),
        "aapl":   _fetch_with_fallback("AAPL",   "aapl.us"),
    }


def _fetch_macro_assets_weekly() -> dict:
    """Gold and DXY 7-day change — Yahoo primary, stooq fallback."""
    return {
        "gold": _fetch_with_fallback("GC=F",     "xauusd"),
        "dxy":  _fetch_with_fallback("DX-Y.NYB", "^dxy"),
    }


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


# ─── New fetchers for private version ────────────────────────────────────────

def _fetch_fear_greed_history() -> list:
    """Fetch last 8 days of Fear & Greed for trajectory."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=8", timeout=6)
        data = r.json().get("data", [])
        return [{"value": int(d.get("value", 0)),
                 "label": d.get("value_classification", "")} for d in data]
    except Exception as e:
        log.warning(f"F&G history fetch failed: {e}")
        return []


def _fetch_global_history() -> dict:
    """Fetch BTC dominance trajectory using CoinGecko 'global' now + computation.
       CoinGecko free tier doesn't expose historical global — we fetch current
       and compute trajectory from BTC mcap vs total mcap approximation."""
    try:
        # Current dominance is in _fetch_global_mcap. For delta, use BTC 7d price
        # change and total mcap 24h change as a proxy. Full historical requires paid.
        r = requests.get(f"{COINGECKO_BASE}/global", timeout=8)
        data = r.json().get("data", {})
        return {
            "btc_dominance":        data.get("market_cap_percentage", {}).get("btc"),
            "mcap_chg_24h":         data.get("market_cap_change_percentage_24h_usd"),
            "active_cryptos":       data.get("active_cryptocurrencies"),
        }
    except Exception as e:
        log.warning(f"Global history fetch failed: {e}")
        return {}


def _fetch_upcoming_macro(modules: dict, days: int = 14) -> list:
    """Get upcoming macro events from FedWatch for next N days."""
    try:
        events = modules["fedwatch"].STATE.get("events", [])
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        upcoming = [ev for ev in events
                    if ev.get("start") and now < ev["start"] <= cutoff]
        upcoming.sort(key=lambda e: e["start"])
        return upcoming[:8]
    except Exception as e:
        log.warning(f"Upcoming macro fetch failed: {e}")
        return []


def _fetch_correl(modules: dict) -> dict:
    """Pull DXY/BTC correlation snapshot from CorrelWatch."""
    try:
        state = modules["correlwatch"].STATE
        return {
            "dxy_chg_24h": state.get("dxy_chg_24h"),
            "btc_chg_24h": state.get("btc_chg_24h"),
            "divergence":  state.get("divergence", ""),
        }
    except Exception:
        return {}


def _fetch_asset_structure(symbol: str) -> dict | None:
    """Fetch 50W/200W EMAs, RSI, and weekly range for an asset."""
    try:
        # Daily candles for range + RSI (Bitget max limit is 200)
        r_daily = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": "1D",
                    "limit": "200", "productType": PRODUCT_TYPE},
            timeout=10,
        )
        d_daily = r_daily.json()
        if d_daily.get("code") != "00000":
            log.warning(f"Structure fetch {symbol}: code={d_daily.get('code')} msg={d_daily.get('msg')}")
            return None
        candles = d_daily.get("data") or []
        if len(candles) < 50:
            log.warning(f"Structure fetch {symbol}: only {len(candles)} candles")
            return None

        closes = [float(c[4]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]

        price = closes[-1]

        # Weekly range (last 7 days)
        week_high = max(highs[-7:])
        week_low  = min(lows[-7:])
        range_pct = (week_high - week_low) / week_low * 100 if week_low > 0 else 0

        # Simple EMA (close enough for a signal)
        def _ema(vals, period):
            if len(vals) < period:
                return None
            k   = 2 / (period + 1)
            ema = sum(vals[:period]) / period
            for v in vals[period:]:
                ema = v * k + ema * (1 - k)
            return ema

        # 50-day and 200-day EMAs on daily closes (proxy for 50W/200W in crypto 24/7 market)
        ema50  = _ema(closes, 50)
        ema200 = _ema(closes, 200)

        # RSI-14
        gains, losses = 0.0, 0.0
        for i in range(1, 15):
            diff = closes[-i] - closes[-i - 1]
            if diff >= 0: gains  += diff
            else:         losses += -diff
        avg_gain = gains / 14
        avg_loss = losses / 14
        rsi = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss > 0 else 100

        return {
            "price":      price,
            "week_high":  week_high,
            "week_low":   week_low,
            "range_pct":  range_pct,
            "ema50":      ema50,
            "ema200":     ema200,
            "vs_ema50":   ((price - ema50) / ema50 * 100) if ema50 else None,
            "vs_ema200": ((price - ema200) / ema200 * 100) if ema200 else None,
            "rsi":        rsi,
        }
    except Exception as e:
        log.warning(f"Structure fetch failed for {symbol}: {e}")
        return None


def _fetch_options_positioning(modules: dict) -> dict:
    """Pull latest options snapshot from OptionsWatch for BTC + ETH."""
    try:
        state = modules["optionswatch"].STATE
        return {
            "btc": state.get("btc", {}) or {},
            "eth": state.get("eth", {}) or {},
        }
    except Exception:
        return {}


def _fetch_stables_marketcap() -> dict:
    """Fetch USDT and USDC current market cap."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids":         "tether,usd-coin",
                "sparkline":   False,
            },
            timeout=10,
        )
        data = r.json()
        result = {}
        for coin in data:
            cid  = coin.get("id")
            mcap = coin.get("market_cap")
            chg  = coin.get("market_cap_change_percentage_24h")
            if cid == "tether":   result["USDT"] = {"mcap": mcap, "chg_24h": chg}
            if cid == "usd-coin": result["USDC"] = {"mcap": mcap, "chg_24h": chg}
        return result
    except Exception as e:
        log.warning(f"Stables mcap fetch failed: {e}")
        return {}


# Sector → category ID mapping (CoinGecko categories)
SECTOR_CATEGORIES = {
    "DeFi":     "decentralized-finance-defi",
    "AI":       "artificial-intelligence",
    "L2":       "layer-2",
    "Gaming":   "gaming",
    "Meme":     "meme-token",
    "RWA":      "real-world-assets-rwa",
}


def _fetch_sector_performance() -> dict:
    """Fetch 7d performance for major sectors from CoinGecko."""
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/categories",
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list):
            return {}
        # Build reverse lookup
        id_to_name = {v: k for k, v in SECTOR_CATEGORIES.items()}
        result = {}
        for cat in data:
            cid = cat.get("id")
            if cid in id_to_name:
                chg = cat.get("market_cap_change_24h")  # Percentage
                # Some categories use 'market_cap_change_24h' as % in CoinGecko
                result[id_to_name[cid]] = {
                    "mcap":     cat.get("market_cap"),
                    "chg_24h":  chg,
                    "vol_24h":  cat.get("volume_24h"),
                }
        return result
    except Exception as e:
        log.warning(f"Sector fetch failed: {e}")
        return {}


# ─── AI narrative ────────────────────────────────────────────────────────────

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def _call_openai(prompt: str, max_tokens: int = 120) -> str:
    """Shared OpenAI caller. Returns text or empty string on failure."""
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set")
        return ""
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            json={
                "model":       OPENAI_MODEL,
                "temperature": 0.3,
                "max_tokens":  max_tokens,
                "messages":    [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        data    = response.json()
        choices = data.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content", "").strip()
    except Exception as e:
        log.warning(f"OpenAI call failed: {e}")
    return ""


def _generate_narrative(data: dict) -> str:
    """Short 3-sentence weekly market narrative."""
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

Be direct, no fluff. Max 40 words total. No emojis."""
    return _call_openai(prompt, max_tokens=120)


def _generate_ascent_commentary(data: dict) -> str:
    """1-2 sentences on current market conditions for ATRb v2 strategy context."""
    prompt = f"""You are a quant analyst briefing copy traders on the ATRb v2 strategy.
Write exactly 1-2 short sentences describing current market conditions relevant to a
4H momentum strategy trading ETH (100% allocation). Focus on: volatility regime, trend strength,
and whether conditions favor or hurt a breakout strategy.

This week's data:
- ETH: {data.get('eth_chg', 'N/A')}%
- BTC: {data.get('btc_chg', 'N/A')}%
- Fear & Greed: {data.get('fg_value', 'N/A')} ({data.get('fg_label', 'N/A')})

Be direct. Max 30 words total. No emojis. No disclaimers."""
    return _call_openai(prompt, max_tokens=80)


# ─── Message builder ──────────────────────────────────────────────────────────

def build_weekly_brief(modules: dict, private: bool = False) -> str:
    now        = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end   = now.strftime("%b %d, %Y")

    # Fetch all data
    crypto   = _fetch_crypto_weekly()
    global_  = _fetch_global_mcap()
    equities     = _fetch_equity_weekly()
    macro_assets = _fetch_macro_assets_weekly()
    fg       = _fetch_fear_greed()
    liqs     = _fetch_liq_summary(modules)
    funding  = _fetch_funding_summary(modules)

    btc = crypto.get("bitcoin", {})
    eth = crypto.get("ethereum", {})

    # ATRb v2 strategy assets
    eth_w = _fetch_asset_weekly("ETHUSDT")

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

    # Pre-compute F&G trajectory (merged into Crypto Market block)
    fg_hist = _fetch_fear_greed_history()
    fg_delta = None
    fg_week  = None
    if len(fg_hist) >= 7 and fg.get("value"):
        fg_week  = fg_hist[6]["value"] if len(fg_hist) > 6 else fg_hist[-1]["value"]
        fg_delta = fg["value"] - fg_week

    # BTC dominance trajectory (merged into Crypto Market block)
    global_hist = _fetch_global_history()
    cur_dom = global_hist.get("btc_dominance")
    btc_chg_7d = btc.get("chg7d") or 0
    dom_trend = "↑" if btc_chg_7d > 0.5 else "↓" if btc_chg_7d < -0.5 else "→"

    # Build header (date range only) — Crypto Market section conditional
    lines = [
        f"📊 *Infinex Capital — Weekly Market Brief*",
        f"_Intelligence provided by MacroWatch 🧠_",
        f"📅 {week_start} → {week_end}",
        "",
    ]

    # Check if we have any crypto data to show
    has_crypto = bool(btc.get("price") or eth.get("price") or mcap_t or cur_dom or fg.get("value"))

    if has_crypto:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "🪙 *CRYPTO MARKET*",
            "",
        ]

        # Group 1: Prices
        if btc.get("price"):
            lines.append(
                f"₿ *BTC*  `${btc['price']:,.0f}`   "
                f"{_chg_emoji(btc.get('chg7d'))} `{_pct(btc.get('chg7d'))}`"
            )
        if eth.get("price"):
            lines.append(
                f"Ξ *ETH*  `${eth['price']:,.2f}`    "
                f"{_chg_emoji(eth.get('chg7d'))} `{_pct(eth.get('chg7d'))}`"
            )

        if btc.get("price") or eth.get("price"):
            lines.append("")

        # Group 2: Market size
        if mcap_t:
            lines.append(f"🌐 *Total Mcap*   `${mcap_t}T`")
        if cur_dom:
            lines.append(f"👑 *BTC Dom*      `{cur_dom:.1f}%`  {dom_trend}")

        if mcap_t or cur_dom:
            lines.append("")

        # Group 3: Sentiment
        if fg.get("value"):
            val = fg["value"]
            if val >= 75:   fg_emoji = "🟢"
            elif val >= 55: fg_emoji = "🟡"
            elif val >= 45: fg_emoji = "🟠"
            else:           fg_emoji = "🔴"
            lines.append(f"🎭 *Fear & Greed*  `{val}` — {fg_emoji} {fg['label']}")
            if fg_delta is not None:
                sign = "+" if fg_delta >= 0 else ""
                arrow = "📈" if fg_delta > 3 else "📉" if fg_delta < -3 else "➡️"
                lines.append(f"   _7d: {sign}{fg_delta} {arrow}_")
            lines.append("")

    # ── Traditional Markets (7D) ─────────────────────────────────────────────
    has_equities = equities and any(v is not None for v in equities.values())
    has_macro    = macro_assets and any(v is not None for v in macro_assets.values())
    if has_equities or has_macro:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "🌍 *TRADITIONAL MARKETS (7D)*",
            "",
        ]
        if has_equities:
            lines.append("📈 *Equities*")
            if equities.get("sp500") is not None:
                e = "📈" if equities["sp500"] >= 0 else "📉"
                lines.append(f"  S&P 500:  {e} `{_pct(equities['sp500'])}`")
            if equities.get("nasdaq") is not None:
                e = "📈" if equities["nasdaq"] >= 0 else "📉"
                lines.append(f"  Nasdaq:   {e} `{_pct(equities['nasdaq'])}`")
            if equities.get("aapl") is not None:
                e = "📈" if equities["aapl"] >= 0 else "📉"
                lines.append(f"  🍎 AAPL:  {e} `{_pct(equities['aapl'])}`")
            lines.append("")

        if has_macro:
            lines.append("💰 *Macro Assets*")
            if macro_assets.get("gold") is not None:
                e = "📈" if macro_assets["gold"] >= 0 else "📉"
                lines.append(f"  🥇 Gold:   {e} `{_pct(macro_assets['gold'])}`")
            if macro_assets.get("dxy") is not None:
                e = "📈" if macro_assets["dxy"] >= 0 else "📉"
                lines.append(f"  💵 DXY:    {e} `{_pct(macro_assets['dxy'])}`")
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

    # Funding rates — PRIVATE ONLY
    if private and funding:
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

    # ═══ SHARED SECTIONS (both public and private) ═══════════════════════════

    # Next Macro Calendar
    upcoming_macro = _fetch_upcoming_macro(modules, days=14)
    if upcoming_macro:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "📅 *NEXT MACRO CALENDAR*",
            "",
        ]
        for ev in upcoming_macro[:6]:
            try:
                ts     = ev["start"].strftime("%a %b %d")
                title  = ev.get("title", "Event")
                impact = (ev.get("impact") or "").lower()
                icon   = "🔥" if impact == "high" else "⚠️" if impact == "medium" else "📌"
                lines.append(f"  {icon} {ts} — {title}")
            except Exception:
                continue
        lines.append("")

    # CorrelWatch — DXY vs BTC
    correl = _fetch_correl(modules)
    if correl.get("dxy_chg_24h") is not None and correl.get("btc_chg_24h") is not None:
        dxy_chg    = correl["dxy_chg_24h"]
        btc_chg    = correl["btc_chg_24h"]
        divergence = correl.get("divergence", "")
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "📡 *DXY vs BTC CORRELATION*",
            "",
            f"  DXY: `{_pct(dxy_chg)}`  |  BTC: `{_pct(btc_chg)}`",
        ]
        if divergence:
            lines.append(f"  _{divergence}_")
        lines.append("")

    # ═══ END SHARED SECTIONS ════════════════════════════════════════════════

    # ATRb v2 strategy — real PnL from last 7 days closed trades
    try:
        from bot.modules.strategyrecap import _fetch_closed_trades
        closed = _fetch_closed_trades()
    except Exception as e:
        log.warning(f"Closed trades fetch failed: {e}")
        closed = []

    # Aggregate by asset
    pnl_by_asset = {"ETHUSDT": 0.0}
    trades_by_asset = {"ETHUSDT": 0}
    for t in closed:
        sym = t.get("symbol", "")
        if sym in pnl_by_asset:
            pnl_by_asset[sym]    += t.get("pnl", 0.0)
            trades_by_asset[sym] += 1
    total_pnl    = sum(pnl_by_asset.values())
    total_trades = sum(trades_by_asset.values())

    ascent_data = {
        "eth_chg":  eth_w if eth_w is not None else "N/A",
        "btc_chg":  round(btc.get("chg7d", 0), 2) if btc.get("chg7d") else "N/A",
        "fg_value": fg.get("value", "N/A"),
        "fg_label": fg.get("label", "N/A"),
    }
    ascent_commentary = _generate_ascent_commentary(ascent_data)

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🤖 *ATRb v2*",
        "",
    ]
    if ascent_commentary:
        lines += [f"_{ascent_commentary}_", ""]

    # Weekly PnL summary
    if total_trades > 0:
        net_e    = "🟢" if total_pnl >= 0 else "🔴"
        net_sign = "+" if total_pnl >= 0 else "-"
        lines.append(f"📊 *Weekly PnL:* {net_e} `{net_sign}${abs(total_pnl):.2f}` ({total_trades} trades)")
    else:
        lines.append("📊 *Weekly PnL:* no trades this week")
        lines.append("_Strategy only fires when volatility + momentum align._")

    # Market context (spot performance — clearly separated)
    if eth_w is not None:
        sign = "+" if eth_w >= 0 else ""
        lines += [
            "",
            "_Market context (7D spot performance):_",
            f"  ETH `{sign}{eth_w:.2f}%`",
        ]

    # ═══ PRIVATE-ONLY SECTIONS ═══════════════════════════════════════════════
    if private:
        # Options positioning (BTC + ETH with gap-to-current calc)
        opt = _fetch_options_positioning(modules)
        btc_opt = opt.get("btc", {}) or {}
        eth_opt = opt.get("eth", {}) or {}

        if btc_opt.get("max_pain") or eth_opt.get("max_pain"):
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "⚙️ *OPTIONS POSITIONING*",
                "",
            ]

            for label, data in [("BTC", btc_opt), ("ETH", eth_opt)]:
                if not data.get("max_pain"):
                    continue
                pain   = data["max_pain"]
                expiry = data.get("expiry_str", "N/A")
                price  = data.get("price")

                line = f"  *{label}*: Expiry `{expiry}` — Max Pain `${pain:,.0f}`"
                if price:
                    gap_pct = (price - pain) / price * 100
                    direction = "above" if gap_pct > 0 else "below"
                    line += f" (`{abs(gap_pct):.1f}%` {direction} current)"
                lines.append(line)
            lines.append("")

        # Stablecoin market cap
        stables = _fetch_stables_marketcap()
        if stables:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "💰 *STABLECOIN MARKET CAP*",
                "",
            ]
            for label in ["USDT", "USDC"]:
                s = stables.get(label)
                if not s:
                    continue
                mcap_b = s['mcap'] / 1e9 if s.get('mcap') else 0
                chg    = s.get('chg_24h') or 0
                e      = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
                sign   = "+" if chg >= 0 else ""
                lines.append(f"  {e} {label}: `${mcap_b:.1f}B`  24h: `{sign}{chg:.2f}%`")
            lines.append("")

        # Sector performance
        sectors = _fetch_sector_performance()
        if sectors:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "🎯 *SECTOR PERFORMANCE (24H)*",
                "",
            ]
            # Sort by 24h change
            sorted_sectors = sorted(
                [(k, v) for k, v in sectors.items() if v.get("chg_24h") is not None],
                key=lambda x: x[1]["chg_24h"],
                reverse=True,
            )
            for name, s in sorted_sectors:
                chg  = s["chg_24h"]
                e    = "📈" if chg > 1 else "📉" if chg < -1 else "➡️"
                sign = "+" if chg >= 0 else ""
                lines.append(f"  {e} {name}: `{sign}{chg:.2f}%`")
            lines.append("")

    # ═══ END PRIVATE SECTIONS ════════════════════════════════════════════════

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"_Published {now.strftime('%A %d %B %Y, %H:%M UTC')}_",
    ]

    return "\n".join(lines)


# ─── Entry point ─────────────────────────────────────────────────────────────

def send_weekly_brief(modules: dict):
    try:
        private_msg = build_weekly_brief(modules, private=True)
        public_msg  = build_weekly_brief(modules, private=False)
    except Exception as e:
        log.exception(f"WeeklyBrief build failed: {e}")
        send_text(f"📊 [WeeklyBrief] ⚠️ Build failed: {str(e)[:200]}")
        return

    # Send FULL version to private group
    send_text(private_msg)

    # Send LITE version to public channel
    if PUBLIC_CHAT_ID:
        try:
            import os as _os
            requests.post(
                f"https://api.telegram.org/bot{_os.getenv('TELEGRAM_TOKEN', '')}/sendMessage",
                json={"chat_id": PUBLIC_CHAT_ID, "text": public_msg,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=10,
            )
            log.info("WeeklyBrief sent to public channel ✅")
        except Exception as e:
            log.warning(f"WeeklyBrief public send failed: {e}")

    log.info("WeeklyBrief sent ✅")
