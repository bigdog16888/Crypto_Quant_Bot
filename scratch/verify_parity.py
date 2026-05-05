
from engine.database import get_all_bots, sync_trades_from_orders, get_bot_status
import logging

logging.basicConfig(level=logging.INFO)

def check_all_bots():
    bots = get_all_bots()
    for b in bots:
        bot_id = b[0]
        name = b[1]
        print(f"\n--- Checking Bot {bot_id}: {name} ---")
        try:
            changed = sync_trades_from_orders(bot_id)
            if changed:
                print(f"✅ Bot {bot_id} synchronized and updated.")
            else:
                print(f"ℹ️ Bot {bot_id} was already in sync.")
            
            status = get_bot_status(bot_id)
            print(f"📊 Final State: Step {status['current_step']}, Phase {status['cycle_phase']}, Invested ${status['total_invested']:.2f}")
        except Exception as e:
            print(f"❌ Error syncing bot {bot_id}: {e}")

if __name__ == "__main__":
    check_all_bots()
