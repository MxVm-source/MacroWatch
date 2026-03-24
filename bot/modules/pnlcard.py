# bot/modules/pnlcard.py
"""
PnL Card — generates a styled PNG trade result card.

Called from main.py when a position closes.
Sends the image via Telegram sendPhoto API.
Falls back to plain text if image generation fails.
"""

import io
import logging
import os

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("pnlcard")

# ─── Telegram ────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

# ─── Fonts ───────────────────────────────────────────────────────────────────

_FONT_DIR  = "/usr/share/fonts/truetype/dejavu/"
_FONT_REG  = _FONT_DIR + "DejaVuSans.ttf"
_FONT_BOLD = _FONT_DIR + "DejaVuSans-Bold.ttf"
_FONT_MONO = _FONT_DIR + "DejaVuSansMono-Bold.ttf"

# Fallback to default if fonts missing (e.g. on Render)
def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

# ─── Colors ──────────────────────────────────────────────────────────────────

BG          = (13,  17,  23)
CARD_BG     = (22,  27,  34)
BORDER_WIN  = (0,  200, 100)
BORDER_LOS  = (220, 50,  50)
TEXT_DIM    = (120, 130, 145)
TEXT_MAIN   = (220, 225, 235)
WIN_GREEN   = (0,  210, 110)
LOSS_RED    = (220, 60,  60)
GOLD        = (255, 200,  50)
DIVIDER     = (40,  48,  60)

W, H = 600, 340


# ─── Card builder ─────────────────────────────────────────────────────────────

def build_card(
    pair:    str,
    side:    str,       # "LONG" or "SHORT"
    entry:   float,
    exit_px: float,
    pnl_pct: float,     # leveraged PnL %
    hold:    str,       # e.g. "14h 32m"
    streak:  str = "",  # e.g. "🔥 5 win streak"
) -> io.BytesIO:

    is_win    = pnl_pct >= 0
    border_c  = BORDER_WIN if is_win else BORDER_LOS
    pnl_color = WIN_GREEN  if is_win else LOSS_RED
    pnl_sign  = "+" if is_win else ""
    side_c    = WIN_GREEN if side == "LONG" else LOSS_RED

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Fonts
    f_pair = _font(_FONT_BOLD, 28)
    f_side = _font(_FONT_BOLD, 18)
    f_lbl  = _font(_FONT_REG,  13)
    f_val  = _font(_FONT_BOLD, 16)
    f_pnl  = _font(_FONT_BOLD, 52)
    f_str  = _font(_FONT_BOLD, 15)
    f_sm   = _font(_FONT_MONO, 13)

    # Border bar
    draw.rectangle([(0, 0), (6, H)], fill=border_c)

    # Card background
    draw.rectangle([(16, 12), (W-12, H-12)], fill=CARD_BG)

    # Header
    draw.text((32, 26), pair,           font=f_pair, fill=TEXT_MAIN)
    draw.text((32, 62), f"● {side}",    font=f_side, fill=side_c)

    # Divider
    draw.rectangle([(32, 92), (W-28, 93)], fill=DIVIDER)

    # Entry / Exit / Hold
    draw.text((32,  106), "ENTRY",           font=f_lbl, fill=TEXT_DIM)
    draw.text((32,  124), f"${entry:,.2f}",  font=f_val, fill=TEXT_MAIN)
    draw.text((200, 106), "EXIT",            font=f_lbl, fill=TEXT_DIM)
    draw.text((200, 124), f"${exit_px:,.2f}",font=f_val, fill=TEXT_MAIN)
    draw.text((370, 106), "HELD",            font=f_lbl, fill=TEXT_DIM)
    draw.text((370, 124), hold,              font=f_val, fill=TEXT_MAIN)

    # Divider
    draw.rectangle([(32, 154), (W-28, 155)], fill=DIVIDER)

    # PnL
    draw.text((32, 165), "P&L",                          font=f_lbl, fill=TEXT_DIM)
    draw.text((32, 182), f"{pnl_sign}{pnl_pct:.2f}%",   font=f_pnl, fill=pnl_color)

    # Streak
    if streak:
        draw.text((32, H-40), streak, font=f_str, fill=GOLD)

    # Watermark
    draw.text((W-115, H-34), "MacroWatch", font=f_sm, fill=(50, 60, 75))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ─── Telegram sender ──────────────────────────────────────────────────────────

def send_card(
    pair:     str,
    side:     str,
    entry:    float,
    exit_px:  float,
    pnl_pct:  float,
    hold:     str,
    streak:   str = "",
    caption:  str = "",
) -> bool:
    """
    Generate and send the P&L card via Telegram sendPhoto.
    Returns True on success, False on failure.
    """
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning("PnLCard: Telegram credentials missing")
        return False

    try:
        buf = build_card(pair, side, entry, exit_px, pnl_pct, hold, streak)
    except Exception as e:
        log.warning(f"PnLCard: build failed: {e}")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={
                "chat_id":    CHAT_ID,
                "caption":    caption or "",
                "parse_mode": "Markdown",
            },
            files={"photo": ("pnl_card.png", buf, "image/png")},
            timeout=15,
        )
        if resp.ok:
            log.info(f"PnLCard sent: {pair} {side} {pnl_pct:+.2f}%")
            return True
        else:
            log.warning(f"PnLCard send failed: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        log.warning(f"PnLCard send error: {e}")
        return False
