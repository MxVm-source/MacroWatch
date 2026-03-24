# bot/modules/weeklyimage.py
"""
WeeklyImage — generates a shareable weekly performance image.

Called from _send_weekly_perf in main.py every Monday at 09:00.
Uses matplotlib for equity curve + trade bars + stat boxes.
Sends via Telegram sendPhoto API.
"""

import io
import logging
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

import requests

log = logging.getLogger("weeklyimage")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

# ─── Colors ──────────────────────────────────────────────────────────────────

BG     = "#0D1117"
CARD   = "#161B22"
GREEN  = "#00D26E"
RED    = "#DC3C3C"
DIM    = "#787F8D"
MAIN   = "#DCE1EB"
ACCENT = "#1F6FEB"


# ─── Builder ─────────────────────────────────────────────────────────────────

def build_image(
    trades:     list,
    week_start: str,
    week_end:   str,
    eth_chg:    float | None = None,
) -> io.BytesIO:
    net_pnl  = sum(t["pnl"] for t in trades) if trades else 0.0
    wins     = sum(1 for t in trades if t["pnl"] > 0)
    losses   = len(trades) - wins
    win_rate = round(wins / len(trades) * 100) if trades else 0

    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl"])

    fig = plt.figure(figsize=(8, 4.4), facecolor=BG)
    gs  = GridSpec(2, 3, figure=fig, left=0.06, right=0.97,
                   top=0.88, bottom=0.12, wspace=0.35, hspace=0.5)

    # Header
    fig.text(0.06, 0.94, f"Weekly Performance  {week_start} -> {week_end}",
             color=MAIN, fontsize=13, fontweight="bold", va="top")
    fig.text(0.97, 0.94, "MacroWatch",
             color=DIM, fontsize=9, va="top", ha="right")

    # Equity curve
    ax_eq = fig.add_subplot(gs[0, :2])
    ax_eq.set_facecolor(CARD)
    if len(equity) > 1:
        x         = list(range(len(equity)))
        curve_col = GREEN if equity[-1] >= 0 else RED
        ax_eq.plot(x, equity, color=curve_col, linewidth=2.5, zorder=3)
        ax_eq.fill_between(x, equity, 0, alpha=0.15, color=curve_col, zorder=2)
        ax_eq.axhline(0, color=DIM, linewidth=0.8, linestyle="--", alpha=0.5)
        ax_eq.set_xlim(0, len(equity) - 1)
        ax_eq.set_xticks(x[1:])
        ax_eq.set_xticklabels([t["date"] for t in trades], color=DIM, fontsize=8)
    else:
        ax_eq.text(0.5, 0.5, "No trades this week",
                   color=DIM, ha="center", va="center", transform=ax_eq.transAxes)
    ax_eq.yaxis.set_tick_params(labelcolor=DIM, labelsize=8)
    ax_eq.set_title("Equity Curve", color=DIM, fontsize=8, pad=4)
    for spine in ax_eq.spines.values():
        spine.set_edgecolor("#2A3240")

    # Trade bars
    ax_tr = fig.add_subplot(gs[0, 2])
    ax_tr.set_facecolor(CARD)
    if trades:
        colors = [GREEN if t["pnl"] > 0 else RED for t in trades]
        ax_tr.bar(range(len(trades)), [t["pnl"] for t in trades],
                  color=colors, width=0.6, zorder=3)
        ax_tr.axhline(0, color=DIM, linewidth=0.8, alpha=0.5)
        ax_tr.set_xticks(range(len(trades)))
        ax_tr.set_xticklabels(
            [t["date"].split(" ")[1] if " " in t["date"] else t["date"]
             for t in trades],
            color=DIM, fontsize=7
        )
    else:
        ax_tr.text(0.5, 0.5, "No trades", color=DIM,
                   ha="center", va="center", transform=ax_tr.transAxes)
    ax_tr.yaxis.set_tick_params(labelcolor=DIM, labelsize=7)
    ax_tr.set_title("Trades", color=DIM, fontsize=8, pad=4)
    for spine in ax_tr.spines.values():
        spine.set_edgecolor("#2A3240")

    # Stat boxes
    eth_str = f"{eth_chg:+.1f}%" if eth_chg is not None else "N/A"
    eth_col = GREEN if (eth_chg or 0) >= 0 else RED

    for i, (lbl, val, col) in enumerate([
        ("NET P&L",  f"${net_pnl:+,.0f}", GREEN if net_pnl >= 0 else RED),
        ("WIN RATE", f"{win_rate}%",       MAIN),
    ]):
        ax_s = fig.add_subplot(gs[1, i])
        ax_s.set_facecolor(CARD)
        ax_s.axis("off")
        ax_s.text(0.5, 0.72, lbl, color=DIM, fontsize=7.5,
                  ha="center", va="center", transform=ax_s.transAxes)
        ax_s.text(0.5, 0.28, val, color=col, fontsize=13, fontweight="bold",
                  ha="center", va="center", transform=ax_s.transAxes)
        for spine in ax_s.spines.values():
            spine.set_edgecolor("#2A3240")

    # Combined trades + ETH box
    ax_r = fig.add_subplot(gs[1, 2])
    ax_r.set_facecolor(CARD)
    ax_r.axis("off")
    ax_r.text(0.25, 0.72, "TRADES", color=DIM, fontsize=7.5,
              ha="center", va="center", transform=ax_r.transAxes)
    ax_r.text(0.25, 0.28, f"{len(trades)} ({wins}W/{losses}L)",
              color=MAIN, fontsize=11, fontweight="bold",
              ha="center", va="center", transform=ax_r.transAxes)
    ax_r.text(0.75, 0.72, "ETH 7D", color=DIM, fontsize=7.5,
              ha="center", va="center", transform=ax_r.transAxes)
    ax_r.text(0.75, 0.28, eth_str, color=eth_col, fontsize=11,
              fontweight="bold", ha="center", va="center",
              transform=ax_r.transAxes)
    for spine in ax_r.spines.values():
        spine.set_edgecolor("#2A3240")

    # Accent line
    fig.add_artist(plt.Line2D([0.06, 0.97], [0.07, 0.07],
                   transform=fig.transFigure, color=ACCENT,
                   linewidth=1.5, alpha=0.6))

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor=BG, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf


# ─── Sender ──────────────────────────────────────────────────────────────────

def send_weekly_image(
    trades:     list,
    week_start: str,
    week_end:   str,
    eth_chg:    float | None = None,
) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        buf = build_image(trades, week_start, week_end, eth_chg)
    except Exception as e:
        log.warning(f"WeeklyImage build failed: {e}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": CHAT_ID},
            files={"photo": ("weekly_perf.png", buf, "image/png")},
            timeout=20,
        )
        if resp.ok:
            log.info("WeeklyImage sent ✅")
            return True
        log.warning(f"WeeklyImage failed: {resp.status_code}")
        return False
    except Exception as e:
        log.warning(f"WeeklyImage send error: {e}")
        return False
