# MacroWatch (Multi-Bot Monorepo)

This repository hosts multiple Telegram trading bots under `bots/`:
- **SwingWatch** — Liquidity × Trendline confluence alerts (with charts)
- **TrumpWatch** — Market-relevant Trump headlines / Truth Social tracker
- **FedWatch** — Federal Reserve event reminders (T−24h / T−1h / T−10m)

## Deploy any bot on Render
1) Create a **Background Worker** on Render
2) Set **Root Directory** to that bot's folder (e.g., `bots/SwingWatch`)
3) **Build:** `pip install --no-cache-dir -r requirements.txt`
4) **Start:** `python -m bot.main`
5) Add the bot-specific environment variables

> Each bot has its own `render.yaml` you can use as a reference.
