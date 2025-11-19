MacroWatch Clean Project (No SwingWatch)
=======================================

Contents:
- bot/main.py             : entrypoint, only TrumpWatch + FedWatch
- bot/utils.py            : Telegram helpers (send_text, get_updates)
- bot/datafeed_bitget.py  : Bitget ticker fetch
- bot/modules/trumpwatch.py       : mock-only Trump headlines (manual/interval)
- bot/modules/trumpwatch_live.py  : real TrumpWatch (X + Truth Social, market filter)
- bot/modules/fedwatch.py         : FedWatch (ICS + BTC/ETH reaction, Brussels time)

To use:
1. Drop the `bot/` folder into your GitHub repo (or merge with your existing one).
2. Set env vars on Render:
   - TELEGRAM_TOKEN
   - CHAT_ID
   - ENABLE_TRUMPWATCH_LIVE=true
   - ENABLE_FEDWATCH=true
   - FED_ICS_URL=https://www.federalreserve.gov/feeds/calendar.ics
   - TW_SOURCE_URL_X=https://nitter.net/TrumpTruthOnX/rss
   - TW_SOURCE_NAME_X=X (Mirror)
   - TW_SOURCE_URL_TS=https://trumpstruth.org/api/latest?limit=10
   - TW_SOURCE_NAME_TS=Truth Social
3. Start command on Render:  python -m bot.main
