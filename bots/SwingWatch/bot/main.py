import os, threading
from bot.scheduler import start_scheduler

def maybe_start_commands():
    if os.getenv("ENABLE_COMMANDS", "true").lower() in ("1","true","yes","on"):
        from bot.commands import run_command_loop
        t = threading.Thread(target=run_command_loop, daemon=True)
        t.start()

if __name__ == "__main__":
    # seed initial mock anchors from env (optional)
    from bot import state
    state.set_mock_price("BTCUSDT", float(os.getenv("MOCK_BTC_CENTER","113000")))
    state.set_mock_price("ETHUSDT", float(os.getenv("MOCK_ETH_CENTER","3984")))
    # drift caps
    state.set_mock_drift(float(os.getenv("MOCK_DRIFT_MIN","3")), float(os.getenv("MOCK_DRIFT_MAX","8")))

    print("🚀 SwingWatch Bot started (4H interval)...", flush=True)
    maybe_start_commands()
    start_scheduler()
