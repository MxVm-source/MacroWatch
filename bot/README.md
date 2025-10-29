# MacroWatchBot — Unified (Mock)

One Telegram bot that posts alerts for:
- 🎯 **SwingWatch** — Liquidity × Trendline confluences (BTC/ETH) with dark charts (every 4h)
- 🍊 **TrumpWatch** — Mock headlines with sentiment & impact (every 15m + on /trumpwatch)
- 🏦 **FedWatch** — Mock Fed events with T−24h / T−1h / T−10m alerts (+ /fedwatch shows next)

## Commands
- `/next` — latest SwingWatch setups
- `/trumpwatch` — force a new TrumpWatch mock headline
- `/tw_recent` — list recent TrumpWatch headlines
- `/fedwatch` — show the **next** Fed event only

## Render (Python env)
- **Root Directory:** `MacroWatch`
- **Build:** `pip install --no-cache-dir -r bot/requirements.txt`
- **Start:** `python -m bot.main`

## Env vars
TELEGRAM_TOKEN=...
CHAT_ID=-1003151813176
ENABLE_SWINGWATCH=true
ENABLE_TRUMPWATCH=true
ENABLE_FEDWATCH=true
SWINGWATCH_DRIFT_MIN=3
SWINGWATCH_DRIFT_MAX=8
SW_BTC_CENTER=113000
SW_ETH_CENTER=3984
TW_INTERVAL_MIN=15
FW_MOCK_MODE=true
FW_TZ_LABEL=CET

On boot, the bot posts: **“✅ MacroWatchBot online — monitoring 🎯 🍊 🏦”**
