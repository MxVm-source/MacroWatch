# template.py

CRYPTO_WATCH_TEMPLATE = """🧠 [CryptoWatch] Weekly Crypto Market Sentiment
📅 Week: {week_start} → {week_end}

🔻 General Mood: {general_mood}
Fear & Greed Index (Weekly Range): {fg_low}–{fg_high}/100 → “{fg_label}”
Weekly Bias: {weekly_bias}
Market Stress: {market_stress}

💰 Price & Market Pressure (This Week)
• Bitcoin (BTC):
  - Weekly close: {btc_close}
  - Weekly change: {btc_weekly_pct}%
  - High / Low: {btc_high} / {btc_low}
  - Key narrative: {btc_narrative}

• Total Crypto Market Cap:
  - Current: {total_mc}
  - Weekly change: {total_mc_weekly_pct}%
  - From recent peak: {total_mc_from_peak_pct}%

• Altcoins:
  - Avg drawdown from recent highs: {alts_avg_drawdown}%
  - Typical range this week: {alts_range_drawdown}%
  - Altcoin tone: {alts_tone}

— — — — — — — — — —

🧾 Contributing Factors (This Week)

1️⃣ Macro Headwinds / Tailwinds
• Main macro theme: {macro_main_theme}
• Key events:
  - {macro_event_1}
  - {macro_event_2}
• Net macro impact on crypto: {macro_impact}

2️⃣ Liquidity & Flows
• Spot volumes: {spot_volume_status}
• Derivatives:
  - Open interest (WoW): {open_interest_wow_pct}%
  - Liquidations (7D): Longs: {long_liq_total} / Shorts: {short_liq_total}
• Exchange net flows: {exchange_net_flows_7d}
• ETF / fund flows: {etf_flows_status}

3️⃣ Regulation & Policy
• U.S. headline this week: {us_reg_highlight}
• EU headline this week: {eu_reg_highlight}
• Other key jurisdiction: {other_reg_highlight}
• Overall regulatory tone: {reg_tone}

4️⃣ Market Psychology
• Retail behavior: {retail_behavior}
• Social/media sentiment: {social_sentiment}
• Dominant emotions: {dominant_emotions}

— — — — — — — — — —

📈 Counterpoint – Opportunity View
• Contrarian perspective: {contrarian_view}
• On-chain:
  - Long-term holders: {lth_behavior}
  - Short-term holders: {sth_behavior}
  - Capitulation signs: {onchain_capitulation_status}
• Structural metrics:
  - Activity trend: {activity_trend}
  - Concentration (whales vs retail): {concentration_comment}

— — — — — — — — — —

✅ Weekly Summary
• One-liner: {weekly_one_liner}
• Core takeaway:
  - {key_takeaway_1}
  - {key_takeaway_2}

• Risk outlook for next week: {next_week_outlook}

📌 Note: This is a sentiment + context report, not financial advice.
"""
