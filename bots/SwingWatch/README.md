# SwingWatch (Mock Dynamic Drift v3 — Trendlines)

Part of the **MacroWatch** multi-bot repo.

## What it does
- 4H scans (00/04/08/12/16/20 UTC)
- Dynamic mock prices (BTC≈113k, ETH≈3984) with drift **±3–8%**
- Liquidity + Trendline (support/resistance) confluence
- Auto Telegram posts with **dark TradingView-style charts**
- `/next` command to view latest setups on demand

## Render (Python env)
- **Root Directory:** `bots/SwingWatch`
- **Build:** `pip install --no-cache-dir -r requirements.txt`
- **Start:** `python -m bot.main`

### Required ENV
TELEGRAM_TOKEN=...
CHAT_ID=...
ENABLE_COMMANDS=true
POST_SCAN_SUMMARY=true
POST_IMAGE_ON_SCAN=true
MOCK_MODE=true
MOCK_AUTO_POST=true
MOCK_DRIFT_MIN=3
MOCK_DRIFT_MAX=8
MOCK_BTC_CENTER=113000
MOCK_ETH_CENTER=3984
PRICE_MONITOR_INTERVAL_SEC=20

## Switch to real data later
- Replace `liquidity.py`, `trendline.py`, and `confluence.find_confluences` with live feeds.
- Keep message/plot formats unchanged.
