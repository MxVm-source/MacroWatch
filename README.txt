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
The public feed is intentionally low-noise. TrumpWatch and FedWatch remain
private sensors. Position monitoring and the remaining private tools continue
independently.

All public text/photo publishing is gated through bot/public/publisher.py.

Default public policy:
- Weekly Market Brief: ON
- Strategy recap: OFF
- Weekly Intel: OFF
- Challenge: OFF
- Heatmap: OFF
- Research: OFF until implemented
- Market alerts: OFF
- Long-Term Relevance: OFF for public publishing; V1 runs in preview mode

Render environment switches:
  PUBLIC_ENABLED=true
  PUBLIC_ENABLE_WEEKLY=true
  PUBLIC_ENABLE_STRATEGY=false
  PUBLIC_ENABLE_INTEL=false
  PUBLIC_ENABLE_CHALLENGE=false
  PUBLIC_ENABLE_HEATMAP=false

  PUBLIC_ENABLE_RESEARCH=false
  PUBLIC_ENABLE_MARKET_ALERTS=false
  PUBLIC_ENABLE_RELEVANCE=false
  PUBLIC_RELEVANCE_PREVIEW=true
  PUBLIC_RELEVANCE_MIN_SCORE=8
  PUBLIC_DRY_RUN=false

Emergency public kill switch:
  PUBLIC_ENABLED=false

Long-Term Relevance Engine
--------------------------
Phase 2 is built for passive investors rather than short-term traders.

TrumpWatch and FedWatch act as sensors. The relevance engine asks whether an
event could materially change the multi-month or multi-year investment
environment through rates, inflation, growth, fiscal policy, trade, regulation
or geopolitics.

V1 safety rules:
- preview mode is ON by default
- no automatic public post while PUBLIC_RELEVANCE_PREVIEW=true
- public relevance publishing is also OFF by default
- scheduled Fed/CPI/NFP/etc. events are candidates only until the outcome is known
- political statements require confirmation before they can become publishable
- trading-language imperatives are blocked by the public publisher

Private commands:
  /relevance       latest passive-investor preview
  /relevance_diag  engine mode and counters

To make future public publishing possible, both switches would eventually need:
  PUBLIC_RELEVANCE_PREVIEW=false
  PUBLIC_ENABLE_RELEVANCE=true

Do not enable those switches until preview output has been reviewed.

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