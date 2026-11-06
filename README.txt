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
JJ US SESSION BRIEFING — 2026-11-06
---------------------------------
Enabled by default in the existing worker (ENABLE_JJWATCH=false disables it).
Private CHAT_ID only; refuses a destination identical to PUBLIC_CHAT_ID.

- Sunday–Thursday 19:00 Europe/Brussels: following-day preparation.
- Selected US high/medium impact events: grouped reminder around T−30 minutes.
- Official publication confirmation: BLS employment/CPI/PPI, BEA GDP/PCE,
  Fed policy statements where the release identity/date can be matched.
- XNYS session open minus 15 minutes: updated synthesis, excluding closed days.
- Conservative surprise-Fed-statement monitoring before the US open.
- /jj gives an on-demand brief; /jj_diag shows source health.

Sources: Fair Economy/Forex Factory weekly JSON for dated calendar and explicitly
attributed secondary consensus; official BLS ICS overrides conflicting BLS dates.
BLS Atom and BEA/Fed RSS confirm publications. No next-week FF export is assumed.
If the weekly feed does not yet cover a requested day, the briefing says coverage
is incomplete. Unsupported numerical actuals/revisions remain unavailable; the
release link and an official excerpt are provided. No invented expectations,
verified CME probabilities, automatic directional signals, or measured Nasdaq /
Treasury / dollar reaction. Corporate earnings and geopolitical headlines are
not comprehensively monitored by this first implementation.

Timing: worker checks every 60 seconds; release feeds accelerate around events.
No second-level publication guarantee. Calendar refresh hourly, BLS schedule six
hourly. Successful delivery and source caches persist in /var/data/jjwatch.sqlite3
(existing disk); JJ_STATE_DIR can override the directory. Historical reminders
and already-past releases are not replayed on startup. Telegram delivery is
acknowledged before marking an alert sent (network ambiguity may still duplicate
a message). One activation notice per version; no secrets are written to logs.

JJ replaces legacy scheduled FedWatch alerts and the incorrectly timed old
market-open alert. Existing morning/weekly modules receive the sourced calendar;
existing unrelated worker modules are preserved. Legacy AI consensus output and
unverified rate-probability output are disabled. No additional paid data service.

Validation: python -m unittest discover -s tests -p 'test_*.py'
            python -m compileall -q bot

BLS NETWORK ACCESS — NOT FULLY OPERATIONAL
Render and GitHub runners both receive HTTP errors for the four official BLS
inputs. The cache refresh workflow has NO schedule or push trigger; its initial
test failed, so automatic retries were disabled. It can be run manually only.
A one-time verified public BLS snapshot is in data/bls-official-cache.json.
Render's fallback validates original URL and retrieval time and refuses feeds
older than two hours (calendar six), so this snapshot is NOT an ongoing feed.

Calendar-based briefings/reminders and Fed/BEA publication monitoring are live.
Ongoing automatic BLS results and official BLS calendar revalidation remain
blocked until a usable source/network is provided. Missing sources/results are
shown in the messages and /jj_diag. This implementation does NOT claim full
post-publication coverage. No paid service has been added.

CLEANUP — 2026-11-06
--------------------
All crypto trading is stopped. The entrypoint now schedules ONLY:
- jjwatch: private JJ session prep, event reminders/confirmed outcomes and US close.
- trumpwatch: filtered traditional-market macro statements, every60s.
- weekly_brief: remaining-week macro calendar, Monday09h Europe/Brussels, private.

Retired: BTC/ETH morning08h/evening21h, ATRb strategy recap, crypto Intel deep dive,
position/whale/challenge polling, old fixed-hour crypto market-open snapshot and
promotional welcomes. Legacy source files remain for reference, but commands can
no longer trigger those crypto reports. No order/trading account action is made.
Public publishing is paused by default (PUBLIC_MANUAL_ONLY=true); the public
INFINEX CAPITAL group is reserved for manually shared strategy content.

New /evening: latest completed cash-session recap, Nasdaq Composite/S&P500 daily
indicative closes versus prior exchange-session closes, official agency releases
where available. Auto-delivery close+10 minutes follows XNYS holidays, early closes
and IANA timezones. No inference from stale quotes or unsupported news causes.
If a course/source is unavailable, the report says so. It is not a real-time wire.
/weekly now reports the remaining US week; /health lists the three actual jobs.

One-time group-description cleanup resolves both configured groups via Telegram,
requires distinct canonical IDs, private INFINEX/Macro title and public username
macrowatchalpha, and edits descriptions only. No public message is sent. Success
markers persist; permission/identity failures are logged and cause no mutation.
