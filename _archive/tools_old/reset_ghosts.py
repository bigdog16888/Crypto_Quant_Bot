import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import reset_bot_after_tp

# Bots identified as ghosts in Round 11
GHOST_BOTS = [32, 33, 34, 35, 36, 37, 38]

print(f"Resetting {len(GHOST_BOTS)} ghost bots...")

for bot_id in GHOST_BOTS:
    try:
        reset_bot_after_tp(bot_id, exit_price=0.0)
        print(f"✅ Reset Bot {bot_id}")
    except Exception as e:
        print(f"❌ Failed to reset Bot {bot_id}: {e}")

print("Done.")
