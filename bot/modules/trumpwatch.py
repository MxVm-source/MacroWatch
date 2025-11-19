from datetime import datetime
from bot.utils import send_text

_recent = []  # store last few mock headlines


def post_mock(force: bool = False):
    """Send a mock Trump headline (for testing only)."""
    text = f"Mock Trump headline at {datetime.utcnow().isoformat(timespec='seconds')}Z"
    msg = (
        "🍊 [TrumpWatch] Mock headline (testing)\n"
        f"🗞️ {text}"
    )
    send_text(msg)
    _recent.append(text)
    if len(_recent) > 10:
        _recent.pop(0)


def show_recent():
    if not _recent:
        send_text("🍊 [TrumpWatch] No recent mock headlines stored.")
        return
    lines = ["🍊 [TrumpWatch] Recent mock headlines:"]
    for t in _recent[-5:]:
        lines.append(f"• {t}")
    send_text("\n".join(lines))
