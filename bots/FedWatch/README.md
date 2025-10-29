# FedWatch (MacroWatch)

Background worker that posts **upcoming Federal Reserve events** at T−24h / T−1h / T−10m. 

## Status
This skeleton is wired for Render and Telegram and includes a heartbeat loop. Wire the actual ICS fetch + scheduling when ready.

## Render (Python env)
- Root Directory: `bots/FedWatch`
- Build: `pip install --no-cache-dir -r requirements.txt`
- Start: `python -m bot.main`

## Env vars
TELEGRAM_TOKEN=...     # bot token for FedWatch
CHAT_ID=...            # target group/channel id
POST_HEARTBEAT=true
HEARTBEAT_MINUTES=60
TZ_LABEL=CET

FED_ICS_URL=https://www.federalreserve.gov/feeds/events.ics  # update if changed
ALERT_TIMINGS=T-24h,T-1h,T-10m                               # future use

## Next steps
- Add ICS parser and cache events
- Schedule reminders at the offsets above
