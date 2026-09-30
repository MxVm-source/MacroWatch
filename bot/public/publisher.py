"""Centralised public-channel publisher.

All public Telegram posts should pass through this module so that one
environment switch can stop the public feed without affecting the private feed.
"""

import logging
import os

import requests

log = logging.getLogger("public.publisher")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


FEATURE_DEFAULTS = {
    "weekly": True,
    "market_alerts": False,
    "research": False,
    "strategy": False,
    "intel": False,
    "challenge": False,
    "heatmap": False,
}


def feature_enabled(feature: str) -> bool:
    """Return whether a public-feed feature is allowed to publish."""
    if not _flag("PUBLIC_ENABLED", True):
        return False
    env_name = f"PUBLIC_ENABLE_{feature.upper()}"
    return _flag(env_name, FEATURE_DEFAULTS.get(feature, False))


LEGACY_WEEKLY_TERMS = (
    "atrb v2",
    "confluence",
    "copy trading",
    "$1k → $100k",
)


def validate_public_text(text: str, feature: str, max_chars: int = 3900) -> tuple[bool, str]:
    """Apply hard publication rules before anything can reach Telegram."""
    if not text or not text.strip():
        return False, "empty message"
    if len(text) > max_chars:
        return False, f"message too long ({len(text)} > {max_chars})"
    if feature == "weekly":
        lowered = text.lower()
        for term in LEGACY_WEEKLY_TERMS:
            if term in lowered:
                return False, f"legacy weekly term blocked: {term}"
    return True, ""


def send_public(
    text: str,
    *,
    feature: str = "weekly",
    parse_mode: str = "Markdown",
    disable_web_page_preview: bool = True,
) -> bool:
    """Send one validated text message to the public Telegram channel."""
    if not feature_enabled(feature):
        log.info("Public feature %s disabled; message suppressed", feature)
        return False

    valid, reason = validate_public_text(text, feature)
    if not valid:
        log.warning("Public message rejected by policy: %s", reason)
        return False

    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("PUBLIC_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Public Telegram credentials are not configured")
        return False

    if _flag("PUBLIC_DRY_RUN", False):
        log.info("PUBLIC_DRY_RUN: suppressed public message: %s", text[:200])
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=10,
        )
        if response.status_code != 200:
            log.warning(
                "Public Telegram send failed HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            return False
        log.info("Public message sent via feature=%s", feature)
        return True
    except Exception as exc:
        log.warning("Public Telegram send failed: %s", exc)
        return False


def send_public_photo(
    image_url: str,
    caption: str,
    *,
    feature: str = "heatmap",
    parse_mode: str = "Markdown",
) -> bool:
    """Send one gated photo post to the public Telegram channel."""
    if not feature_enabled(feature):
        log.info("Public feature %s disabled; photo suppressed", feature)
        return False

    valid, reason = validate_public_text(caption, feature, max_chars=950)
    if not valid:
        log.warning("Public photo rejected by policy: %s", reason)
        return False

    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("PUBLIC_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Public Telegram credentials are not configured")
        return False

    if _flag("PUBLIC_DRY_RUN", False):
        log.info("PUBLIC_DRY_RUN: suppressed public photo: %s", caption[:200])
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            json={
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
                "parse_mode": parse_mode,
            },
            timeout=15,
        )
        if response.status_code != 200:
            log.warning(
                "Public Telegram photo failed HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            return False
        log.info("Public photo sent via feature=%s", feature)
        return True
    except Exception as exc:
        log.warning("Public Telegram photo failed: %s", exc)
        return False