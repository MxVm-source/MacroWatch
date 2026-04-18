# bot/modules/heatmapwatch.py
"""
HeatmapWatch — BTC Liquidation Heatmap

Fetches liquidation heatmap screenshots from CoinGlass via Apify
(hamdo/coinglass-liquidation-heatmap actor).

Usage:
  - /heatmap [coin]           — on-demand (private group)
  - send_weekly_heatmap()     — called from Monday Weekly Brief (public)

Cost: ~$0.006 per heatmap. Very cheap.

Env: APIFY_API_TOKEN
"""

import logging
import os
from datetime import datetime, timezone

import requests

from bot.utils import send_text

log = logging.getLogger("heatmapwatch")

APIFY_TOKEN       = os.getenv("APIFY_API_TOKEN", "")
APIFY_ACTOR_ID    = "hamdo~coinglass-liquidation-heatmap"
APIFY_BASE        = "https://api.apify.com/v2"
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("CHAT_ID", "")
PUBLIC_CHAT_ID    = os.getenv("PUBLIC_CHAT_ID", "")


def _fetch_heatmap(coin: str = "BTC") -> dict | None:
    """
    Run the Apify actor synchronously and return dataset items.
    Returns dict with {image_url, metadata} or None on failure.
    """
    if not APIFY_TOKEN:
        log.error("APIFY_API_TOKEN not set")
        return None

    # Use run-sync-get-dataset-items: waits for completion, returns dataset
    url = (f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
           f"?token={APIFY_TOKEN}")

    payload = {
        "coin":      coin.upper(),
        "type":      "symbol",
        "width":     1920,
        "height":    1080,
        "waitTime":  5,
        "headless":  True,
    }

    try:
        log.info(f"HeatmapWatch: requesting {coin} heatmap from Apify...")
        r = requests.post(url, json=payload, timeout=90)
        if r.status_code != 200:
            log.warning(f"Apify returned HTTP {r.status_code}: {r.text[:200]}")
            return None

        items = r.json()
        if not items or not isinstance(items, list):
            log.warning(f"Apify returned empty/invalid dataset: {items}")
            return None

        item = items[0]

        # Actor returns image URL in key-value store
        image_url = (item.get("imageUrl")
                     or item.get("image_url")
                     or item.get("url"))
        if not image_url:
            log.warning(f"No image URL in Apify response. Keys: {list(item.keys())}")
            return None

        log.info(f"HeatmapWatch: got image URL for {coin}")
        return {
            "image_url":  image_url,
            "coin":       coin.upper(),
            "captured":   item.get("timestamp") or item.get("captured_at"),
            "raw":        item,
        }

    except requests.exceptions.Timeout:
        log.warning(f"Apify timeout for {coin} heatmap")
        return None
    except Exception as e:
        log.warning(f"Apify fetch failed for {coin}: {e}")
        return None


def _send_photo(chat_id: str, image_url: str, caption: str) -> bool:
    """Send a photo to Telegram by URL."""
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            json={
                "chat_id":    chat_id,
                "photo":      image_url,
                "caption":    caption,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"Telegram sendPhoto HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"Telegram sendPhoto failed: {e}")
        return False


# ─── Public API ──────────────────────────────────────────────────────────────

def send_heatmap(coin: str = "BTC", target: str = "private") -> bool:
    """
    Fetch and send heatmap to specified target.
    target: 'private' or 'public'
    Returns True on success.
    """
    coin = coin.upper().strip()

    # Notify user that we're working on it
    if target == "private":
        send_text(f"🔥 Fetching {coin} liquidation heatmap from CoinGlass — takes ~30s...")

    result = _fetch_heatmap(coin)
    if not result:
        if target == "private":
            send_text(f"🔥 [HeatmapWatch] Could not fetch {coin} heatmap. Check APIFY_API_TOKEN.")
        return False

    now = datetime.now(timezone.utc)

    # Build caption based on target
    if target == "public":
        caption = (
            f"🔥 *Infinex Capital — {coin} Liquidation Heatmap*\n"
            f"_Intelligence provided by MacroWatch 🧠_\n\n"
            f"_Source: CoinGlass · {now.strftime('%b %d, %Y')}_\n\n"
            f"Yellow/red zones show where liquidation clusters sit. "
            f"Price tends to gravitate toward these levels — "
            f"especially the largest ones."
        )
        chat_id = PUBLIC_CHAT_ID
    else:
        caption = (
            f"🔥 *{coin} Liquidation Heatmap*\n"
            f"_Source: CoinGlass · {now.strftime('%Y-%m-%d %H:%M UTC')}_\n\n"
            f"Yellow/red = large liquidation clusters.\n"
            f"Price often sweeps these zones."
        )
        chat_id = TELEGRAM_CHAT_ID

    ok = _send_photo(chat_id, result["image_url"], caption)
    if not ok and target == "private":
        # Fallback: send URL as text if photo send fails
        send_text(f"🔥 {coin} Heatmap: {result['image_url']}")
        return True

    log.info(f"HeatmapWatch: sent {coin} heatmap to {target}")
    return ok


def send_weekly_heatmap():
    """Called from weekly brief — sends BTC heatmap to public channel."""
    send_heatmap("BTC", target="public")
