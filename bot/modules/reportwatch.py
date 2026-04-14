# bot/modules/reportwatch.py
"""
ReportWatch — 7-day PrimeWatch trade report

Command: /report
Shows last 7 days of closed trades across ETH/BNB/SOL with
win rate, net PnL, best/worst trade — designed to be shareable.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

from bot.utils import send_text
from bot.datafeed_bitget import (
    _signed_request,
    _to_float,
    BITGET_PRODUCT_TYPE,
    BITGET_API_KEY,
)

log = logging.getLogger("reportwatch")

SYMBOLS = ["ETHUSDT", "BNBUSDT", "SOLUSDT"]


def _fetch_trades(days: int = 7) -> list:
    now      = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(now.timestamp() * 1000)
    trades   = []

    for sym in SYMBOLS:
        try:
            res = _signed_request(
                "GET",
                "/api/v2/mix/order/history",
                params={
                    "symbol":      sym,
                    "productType": BITGET_PRODUCT_TYPE,
                    "startTime":   str(start_ms),
                    "endTime":     str(end_ms),
                    "limit":       "100",
                }
            )
            orders = ((res.get("data") or {}).get("orderList") or [])
            for o in orders:
                state      = (o.get("state") or "").lower()
                trade_side = (o.get("tradeSide") or o.get("side") or "").lower()
                pnl_raw    = o.get("pnl") or o.get("realizedPL") or o.get("profit") or ""
                if state != "filled":
                    continue
                if "close" not in trade_side and "reduce" not in trade_side:
                    continue
                try:
                    pnl = float(pnl_raw)
                except Exception:
                    continue
                try:
                    ctime    = int(o.get("cTime") or o.get("uTime") or 0)
                    date_str = datetime.fromtimestamp(ctime / 1000, tz=timezone.utc).strftime("%b %d")
                    ts       = ctime
                except Exception:
                    date_str = "—"
                    ts       = 0

                trades.append({
                    "pnl":    pnl,
                    "date":   date_str,
                    "symbol": sym.replace("USDT", ""),
                    "ts":     ts,
                })
        except Exception as e:
            log.warning(f"Trade fetch failed for {sym}: {e}")

    trades.sort(key=lambda x: x.get("ts", 0))
    return trades


def build_report() -> str:
    if not BITGET_API_KEY:
        return "🔱 [PrimeWatch] ⚠️ API credentials not set."

    now    = datetime.now(timezone.utc)
    trades = _fetch_trades(days=7)

    week_start = (now - timedelta(days=7)).strftime("%b %d")
    week_end   = now.strftime("%b %d")

    if not trades:
        return (
            f"🔱 *PrimeWatch — 7-Day Report*\n"
            f"📅 {week_start} → {week_end}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"No closed trades this week."
        )

    net_pnl  = sum(t["pnl"] for t in trades)
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] <= 0]
    win_rate = round(len(wins) / len(trades) * 100)
    best     = max(trades, key=lambda x: x["pnl"])
    worst    = min(trades, key=lambda x: x["pnl"])

    net_emoji = "📈" if net_pnl >= 0 else "📉"
    net_sign  = "+" if net_pnl >= 0 else ""

    # Trade log lines
    trade_lines = []
    for t in trades:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        s = "+" if t["pnl"] > 0 else ""
        trade_lines.append(f"{e} {t['symbol']}  {s}${t['pnl']:.2f}  —  {t['date']}")

    lines = [
        f"🔱 *PrimeWatch — 7-Day Report*",
        f"📅 {week_start} → {week_end}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"Trades:    {len(trades)}  ({len(wins)}W / {len(losses)}L)",
        f"Win rate:  {win_rate}%",
        f"Net PnL:   {net_emoji} {net_sign}${abs(net_pnl):.2f}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 Trade Log",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    lines += trade_lines
    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 Best:   +${best['pnl']:.2f}  {best['symbol']}  {best['date']}",
        f"💀 Worst:  ${worst['pnl']:.2f}  {worst['symbol']}  {worst['date']}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"_PrimeWatch · ETH · BNB · SOL · 4H_",
    ]

    return "\n".join(lines)


def show_report():
    try:
        msg = build_report()
        send_text(msg)
    except Exception as e:
        log.exception(f"ReportWatch failed: {e}")
        send_text(f"🔱 [Report] ⚠️ Error: {str(e)[:200]}")
