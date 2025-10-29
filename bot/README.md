# MacroWatch — Clean Rebuild (Bitget + Binance Liquidity)
Modules:
- 🎯 SwingWatch — Bitget 4H structure + Binance liquidity (orderbook walls + recent liquidations)
- 🍊 TrumpWatch — High-impact only (>= 0.7), 6h dedupe, /trumpwatch [force]
- 🏦 FedWatch — Mock T-24h / T-1h / T-10m

Render:
- Root Dir: MacroWatch
- Build: pip install --no-cache-dir -r bot/requirements.txt
- Start: python -m bot.main

Env:
TELEGRAM_TOKEN=...
CHAT_ID=-1003151813176
ENABLE_SWINGWATCH=true
ENABLE_TRUMPWATCH=true
ENABLE_FEDWATCH=true
BITGET_SYMBOLS=BTCUSDT_UMCBL,ETHUSDT_UMCBL
BITGET_GRANULARITY_SEC=14400
LIQ_THRESHOLD_USD=150000000
LIQ_PROXIMITY_PCT=0.6
BIN_DEPTH_LIMIT=1000
BIN_BUCKET_BTC=100
BIN_BUCKET_ETH=10
TW_INTERVAL_MIN=15

Commands:
/next, /trumpwatch, /tw_recent, /fedwatch
