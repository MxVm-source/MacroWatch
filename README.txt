MacroWatch
==========

MacroWatch is the market-intelligence engine behind the INFINEX private and
public Telegram feeds.

Runtime
-------
- GitHub: source of truth and CI
- Render: long-running Background Worker
- APScheduler: polling and scheduled jobs inside the worker
- Telegram: private trader feed + read-only public INFINEX feed

Start command:
  python -m bot.main

Public Feed v2
--------------
The public feed is intentionally low-noise. Private modules such as TrumpWatch,
FedWatch, VIX, funding, OI and positions continue to run independently.

All public text/photo publishing is gated through bot/public/publisher.py.

Default public policy:
- Weekly Market Brief: ON
- Strategy recap: OFF
- Weekly Intel: OFF
- Challenge: OFF
- Heatmap: OFF
- Research: OFF until implemented
- Market alerts: OFF until implemented

Render environment switches:
  PUBLIC_ENABLED=true
  PUBLIC_ENABLE_WEEKLY=true
  PUBLIC_ENABLE_STRATEGY=false
  PUBLIC_ENABLE_INTEL=false
  PUBLIC_ENABLE_CHALLENGE=false
  PUBLIC_ENABLE_HEATMAP=false

  PUBLIC_ENABLE_RESEARCH=false
  PUBLIC_ENABLE_MARKET_ALERTS=false
  PUBLIC_DRY_RUN=false

Emergency public kill switch:
  PUBLIC_ENABLED=false

Core Telegram environment:
  TELEGRAM_TOKEN
  CHAT_ID
  PUBLIC_CHAT_ID

Private feature switches:
  ENABLE_ELITE_POSITIONWATCH=false

Set ENABLE_ELITE_POSITIONWATCH=true only after valid Elite Bitget credentials
have been restored.

Validation
----------
GitHub Actions runs on pull requests and on main/public-feed-v2:
  python -m compileall -q bot
  python -m unittest discover -s tests -p "test_*.py"

Private-feed behavior is not changed by the public kill switch.