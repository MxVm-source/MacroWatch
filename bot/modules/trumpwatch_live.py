import threading
from bot.modules import trumpwatch_live

print("🍊 Starting TrumpWatch Live (dual-source)...")
threading.Thread(target=trumpwatch_live.run_loop, daemon=True).start()

# Keep alive forever
import time
while True:
    time.sleep(60)
