import os
import sys
import platform
import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from bot.utils import send_text, get_updates
import bot.modules.fedwatch as fedwatch
import bot.modules.cryptowatch as cryptowatch
import bot.modules.cryptowatch_daily as cryptowatch_daily

from bot.datafeed_bitget import get_position_report_safe

# ✅ Use LIVE TrumpWatch as the source of truth
import bot.modules.trumpwatch_live as trumpwatch_live

STARTED_AT_UTC = datetime.now(timezone.utc)


def _fmt_uptime() -> str:
    delta = datetime.now(timezone.utc) - STARTED_AT_UTC
    sec = int(delta.total_seconds())
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _compute_levels_from_candles(candles, lookback=48):
    """
    Simple S/R from recent 4H candles:
    - support = min(low)
    - resistance = max(high)
    - last = last close
    """
    if not candles:
        return None
    lb = candles[-lookback:] if len(candles) > lookback else candles
    support = min(c["low"] for c in lb)
    resistance = max(c["high"] for c in lb)
    last = candles[-1]["close"]
    return {"support": support, "resistance": resistance, "last": last}


def _atr_simple(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev["close"]),
            abs(c["low"] - prev["close"]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _build_plan(symbol: str):
    """
    Uses TradeWatch checklist + levels to build a clean plan.
    NOTE: If you pause TradeWatch entirely, you can later replace this with BotWatch plans.
    Returns dict: {symbol,last,bias,entry_zone,sl,tp_list,notes}
    """
    from bot.modules import tradewatch as tw

    sym = symbol.replace(".P", "").upper()
    candles = tw.fetch_candles_4h(sym, limit=220)
    if not candles:
        return {"symbol": sym, "error": "No candles returned."}

    levels = _compute_levels_from_candles(candles, lookback=48)
    atr = _atr_simple(candles, 14)
    chk = tw.evaluate_checklist(sym)

    last = levels["last"]
    sup = levels["support"]
    res = levels["resistance"]

    bias = chk.bias
    atr_buf = max(atr * 0.35, last * 0.0015)  # safety buffer

    if bias == "LONG":
        entry_lo = sup + atr_buf * 0.2
        entry_hi = sup + atr_buf * 1.2
        sl = sup - atr_buf * 1.2
        tp1 = last + (res - last) * 0.35
        tp2 = last + (res - last) * 0.70
        tp3 = res
        notes = "Prefer long only after sweep/reclaim + reaction wick on the 4H area."
    elif bias == "SHORT":
        entry_lo = res - atr_buf * 1.2
        entry_hi = res - atr_buf * 0.2
        sl = res + atr_buf * 1.2
        tp1 = last - (last - sup) * 0.35
        tp2 = last - (last - sup) * 0.70
        tp3 = sup
        notes = "Prefer short only after failure to reclaim resistance + bearish reaction at/into FVG."
    else:
        entry_lo = sup + atr_buf * 0.2
        entry_hi = sup + atr_buf * 1.0
        sl = sup - atr_buf * 1.2
        tp1 = last
        tp2 = last + (res - last) * 0.50
        tp3 = res
        notes = "Neutral range: take longs near support only; avoid mid-range chop."

    return {
        "symbol": sym,
        "last": last,
        "support": sup,
        "resistance": res,
        "atr": atr,
        "checklist_status": chk.status,
        "bias": bias,
        "score": f"{chk.score}/{chk.max_score}",
        "entry_zone": (entry_lo, entry_hi),
        "sl": sl,
        "tps": [tp1, tp2, tp3],
        "notes": notes,
    }


def start_scheduler():
    """Start jobs for FedWatch loop, CryptoWatch, and optional TradeWatch."""
    sched = BackgroundScheduler(timezone=os.getenv("TIMEZONE", "Europe/Brussels"))

    # ✅ Guard CryptoWatch Daily so the whole bot never crashes if it's missing main()
    cw_daily_fn = getattr(cryptowatch_daily, "main", None)
    if cw_daily_fn is None:
        print(
            f"⚠️ CryptoWatch Daily disabled: cryptowatch_daily has no main() "
            f"(loaded from {getattr(cryptowatch_daily, '__file__', 'unknown')})",
            flush=True,
        )

    # 🏦 FedWatch alerts (ICS + BTC/ETH reaction)
    if os.getenv("ENABLE_FEDWATCH", "true").lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=fedwatch.schedule_loop, daemon=True).start()

    # 📉 CryptoWatch Daily – mini brief before U.S. market open
    if cw_daily_fn and os.getenv("ENABLE_CRYPTOWATCH_DAILY", "true").lower() in ("1", "true", "yes", "on"):
        sched.add_job(
            cw_daily_fn,
            "cron",
            hour=15,
            minute=28,
            id="cryptowatch_daily_task",
            max_instances=1,
            replace_existing=True,
        )

    # 📊 CryptoWatch Weekly – full weekly sentiment report
    if os.getenv("ENABLE_CRYPTOWATCH_WEEKLY", "true").lower() in ("1", "true", "yes", "on"):
        sched.add_job(
            cryptowatch.main,
            "cron",
            day_of_week="sun",
            hour=18,
            minute=0,
            id="cryptowatch_weekly_task",
            max_instances=1,
            replace_existing=True,
        )

    # 📘 TradeWatch – (PAUSED by default)
    if os.getenv("TRADEWATCH_ENABLED", "0") == "1":
        from bot.modules.tradewatch import start_tradewatch, start_ai_setup_alerts

        threading.Thread(target=start_tradewatch, args=(send_text,), daemon=True).start()

        if os.getenv("TRADEWATCH_AI_ALERTS", "0") == "1":
            threading.Thread(target=start_ai_setup_alerts, args=(send_text,), daemon=True).start()

        # ✅ TP hit watcher (TP1/TP2/TP3 updates)
        if os.getenv("TRADEWATCH_TP_ALERTS", "0") == "1":
            from bot.modules.tradewatch import start_tp_hit_watcher
            threading.Thread(target=start_tp_hit_watcher, args=(send_text,), daemon=True).start()

    sched.start()
    return sched


def command_loop():
    """Telegram commands for MacroWatch."""
    offset = None
    while True:
        data = get_updates(offset=offset, timeout=20)

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}

            text_raw = (msg.get("text") or "").strip()
            text = text_raw.lower()

            chat = str(msg.get("chat", {}).get("id"))
            if not text_raw or chat != str(os.getenv("CHAT_ID")):
                continue

            # ✅ HELP
            if text.startswith("/help"):
                send_text(
                    "🤖 *MacroWatch – Command Guide*\n\n"
                    "🏦 *FedWatch*\n"
                    "/fedwatch – Next Fed event\n"
                    "/fed_diag – FedWatch diagnostics\n\n"
                    "🍊 *TrumpWatch (LIVE)*\n"
                    "/trumpwatch – Trigger an immediate live poll\n"
                    "/tw_recent – (coming back soon) show recent alerts\n\n"
                    "🧠 *AI Strategy*\n"
                    "/ai – Strategy rules (quick)\n"
                    "/levels – Key BTC/ETH support & resistance\n"
                    "/plan – Clean AI trade plan (BTC & ETH)\n\n"
                    "📊 *Positions*\n"
                    "/position – Current Bitget futures positions\n\n"
                    "📊 *CryptoWatch*\n"
                    "/cw_daily – Daily market brief\n"
                    "/cw_weekly – Weekly sentiment\n\n"
                    "🩺 *System*\n"
                    "/health – Bot health + uptime\n"
                )

            # 🧠 AI STRATEGY QUICK
            elif text.startswith("/ai"):
                send_text(
                    "🧠 *AI Strategy (BTC/ETH)*\n"
                    "• 📈 Structure first (HH/HL = long, LH/LL = short)\n"
                    "• 🧲 Liquidity sweep + reclaim = best entries\n"
                    "• 🕳️ FVG reaction = confirmation (wick + close)\n"
                    "• 🎯 Scale out in 2–3 TPs, protect capital\n"
                    "• 🛡️ Invalidation (SL) beyond key S/R + buffer\n"
                    "• ⚠️ If structure is mixed → wait or scalp edges only\n"
                )

            # 📌 LEVELS (BTC/ETH)
            elif text.startswith("/levels"):
                try:
                    from bot.modules import tradewatch as tw
                    btc = tw.fetch_candles_4h("BTCUSDT", limit=220)
                    eth = tw.fetch_candles_4h("ETHUSDT", limit=220)

                    b = _compute_levels_from_candles(btc, lookback=48)
                    e = _compute_levels_from_candles(eth, lookback=48)

                    if not b or not e:
                        send_text("📌 [Levels] Not enough candle data yet.")
                    else:
                        send_text(
                            "📌 *Key Levels (4H)*\n\n"
                            f"₿ BTCUSDT\n"
                            f"• Last: {b['last']:.0f}\n"
                            f"• Support: {b['support']:.0f}\n"
                            f"• Resistance: {b['resistance']:.0f}\n\n"
                            f"Ξ ETHUSDT\n"
                            f"• Last: {e['last']:.0f}\n"
                            f"• Support: {e['support']:.0f}\n"
                            f"• Resistance: {e['resistance']:.0f}\n"
                        )
                except Exception as e:
                    send_text(f"📌 [Levels] Error: {e}")

            # 🧠 PLAN (BTC/ETH)
            elif text.startswith("/plan"):
                try:
                    b = _build_plan("BTCUSDT")
                    e = _build_plan("ETHUSDT")

                    if b.get("error") or e.get("error"):
                        send_text(f"🧠 [Plan] Error: {b.get('error') or e.get('error')}")
                    else:
                        send_text(
                            "🧠 *AI Trade Plan (4H)*\n\n"
                            f"₿ BTCUSDT\n"
                            f"• Status: {b['checklist_status']} | Bias: {b['bias']} | Score: {b['score']}\n"
                            f"• Key: S {b['support']:.0f} / R {b['resistance']:.0f} | Last {b['last']:.0f}\n"
                            f"• Entry: {b['entry_zone'][0]:.0f} – {b['entry_zone'][1]:.0f}\n"
                            f"• SL: {b['sl']:.0f}\n"
                            f"• TP1: {b['tps'][0]:.0f} | TP2: {b['tps'][1]:.0f} | TP3: {b['tps'][2]:.0f}\n"
                            f"• Notes: {b['notes']}\n\n"
                            f"Ξ ETHUSDT\n"
                            f"• Status: {e['checklist_status']} | Bias: {e['bias']} | Score: {e['score']}\n"
                            f"• Key: S {e['support']:.0f} / R {e['resistance']:.0f} | Last {e['last']:.0f}\n"
                            f"• Entry: {e['entry_zone'][0]:.0f} – {e['entry_zone'][1]:.0f}\n"
                            f"• SL: {e['sl']:.0f}\n"
                            f"• TP1: {e['tps'][0]:.0f} | TP2: {e['tps'][1]:.0f} | TP3: {e['tps'][2]:.0f}\n"
                            f"• Notes: {e['notes']}"
                        )
                except Exception as e:
                    send_text(f"🧠 [Plan] Error: {e}")

            # 🍊 TrumpWatch (LIVE)
            elif text.startswith("/trumpwatch"):
                try:
                    trumpwatch_live.poll_once()
                    send_text("🍊 [TrumpWatch] Live poll executed.")
                except Exception as e:
                    send_text(f"🍊 [TrumpWatch] Error running live poll: {e}")

            elif text.startswith("/tw_recent"):
                send_text("🍊 [TrumpWatch] Recent view is coming back soon (live mode).")

            # 🏦 FedWatch
            elif text.startswith("/fedwatch"):
                fedwatch.show_next_event()

            elif text.startswith("/fed_diag"):
                fedwatch.show_diag()

            # 📊 CryptoWatch
            elif text.startswith("/cw_daily"):
                fn = getattr(cryptowatch_daily, "main", None)
                if fn:
                    fn()
                else:
                    send_text("⚠️ CryptoWatch Daily is disabled (cryptowatch_daily.main missing).")

            elif text.startswith("/cw_weekly"):
                cryptowatch.main()

            # 📘 Position report (Bitget)
            elif text.startswith("/position"):
                out = get_position_report_safe()
                send_text(out)

            # ⏸️ TradeWatch commands (PAUSED)
            elif text.startswith("/tradewatch_status") or text.startswith("/setup_status") or text.startswith("/tp_status") or text.startswith("/checklist"):
                send_text("⏸️ TradeWatch is paused while BotWatch takes over execution. Use BotWatch commands in the BotWatch group/bot.")
                continue

            # 🩺 HEALTH
            elif text.startswith("/health"):
                try:
                    send_text(
                        "🩺 *MacroWatch Health*\n"
                        f"• Uptime: {_fmt_uptime()}\n"
                        f"• Started (UTC): {STARTED_AT_UTC.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"• Python: {sys.version.split()[0]} | {platform.system()} {platform.release()}\n"
                    )
                except Exception as e:
                    send_text(f"🩺 [Health] Error: {e}")


if __name__ == "__main__":
    # ✅ Removed boot_banner() to avoid startup Telegram spam

    start_scheduler()

    # Start TrumpWatch Live in a dedicated thread (recommended)
    try:
        if os.getenv("ENABLE_TRUMPWATCH_LIVE", "true").lower() in ("1", "true", "yes", "on"):
            threading.Thread(target=trumpwatch_live.run_loop, daemon=True).start()
            print("🍊 TrumpWatch Live started ✅", flush=True)
        else:
            print("🍊 TrumpWatch Live disabled", flush=True)
    except Exception as e:
        print("⚠️ Error starting TrumpWatch Live:", e, flush=True)

    # Telegram command listener
    threading.Thread(target=command_loop, daemon=True).start()

    # Keep service alive
    while True:
        time.sleep(3600)