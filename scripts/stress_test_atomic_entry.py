import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import time
import threading
from engine.database import update_bot_config_value

def run_stress_test():
    """
    Triggers a simultaneous entry signal for multiple bots on the same pair
    by setting their 'buy_signal_strength' to 1.0 at the same time.
    """
    # Bots 32, 33, 34 all trade BTC/USDC
    target_bots = [32, 33, 34]
    print(f"--- Starting Atomic Entry Stress Test on bots: {target_bots} ---")

    # 1. Reset their signal strength to 0 to ensure they are idle
    print("Step 1: Resetting signal strengths to 0.0...")
    for bot_id in target_bots:
        update_bot_config_value(bot_id, 'buy_signal_strength', 0.0)
    
    print("Waiting 5 seconds for bots to process the reset...")
    time.sleep(5)

    # 2. Trigger all bots simultaneously
    print("Step 2: Triggering BUY signal for all bots simultaneously...")
    threads = []
    for bot_id in target_bots:
        thread = threading.Thread(target=update_bot_config_value, args=(bot_id, 'buy_signal_strength', 1.0))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()
        
    print("--- Stress test triggered. Monitor engine.log for results. ---")
    print("Expected outcome: ONE bot acquires the lock and enters. The others log 'LOCK DENIED'.")

if __name__ == "__main__":
    run_stress_test()
