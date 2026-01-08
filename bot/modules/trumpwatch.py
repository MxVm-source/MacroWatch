"""
Compatibility shim.

Some MacroWatch modules still import `bot.modules.trumpwatch`.
TrumpWatch source of truth is now `trumpwatch_live`.
"""

from bot.modules import trumpwatch_live


def post_mock(force: bool = False):
    # Backwards compatible entrypoint used by older code paths.
    # `force` is ignored in live mode for now.
    return trumpwatch_live.poll_once()


def show_recent():
    # If you later add a real recent cache in trumpwatch_live, you can forward it here.
    # For now keep it honest.
    from bot.utils import send_text
    send_text("🍊 [TrumpWatch] Recent view is not enabled in live mode yet.")