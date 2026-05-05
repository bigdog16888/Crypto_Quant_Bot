from engine.database import sync_trades_from_orders, get_connection
import logging

# Setup logging to see the DNA-WIPE or other logs
logging.basicConfig(level=logging.INFO)

bot_ids = [10016, 10022, 10021, 100002, 10020, 100000, 10017]
for bid in bot_ids:
    try:
        print(f"Syncing bot {bid}...")
        changed = sync_trades_from_orders(bid)
        print(f"Bot {bid} sync result: {'Changed' if changed else 'No change needed'}")
    except Exception as e:
        print(f"FAILED sync for bot {bid}: {e}")
