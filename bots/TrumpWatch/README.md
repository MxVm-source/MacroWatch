# TrumpWatch (MacroWatch)

Background worker that posts **market-relevant Trump headlines / Truth Social posts** with dedupe and sentiment filtering.

## Status
This skeleton is wired for Render and Telegram and includes a heartbeat loop so you can test deployment. 
Replace the polling logic with your real feed when ready.

## Render (Python env)
- Root Directory: `bots/TrumpWatch`
- Build: `pip install --no-cache-dir -r requirements.txt`
- Start: `python -m bot.main`

## Env vars
TELEGRAM_TOKEN=...     # bot token for TrumpWatch
CHAT_ID=...            # target group/channel id
POST_HEARTBEAT=true    # optional online ping every hour
HEARTBEAT_MINUTES=60   # frequency of pings
TZ_LABEL=CET           # label in the heartbeat message

## Next steps
- Replace the loop with your feed collector:
  - Truth Social mirror API or RSS
  - News sources with finance relevance filters
- Implement dedupe + cooldown to avoid repeats
