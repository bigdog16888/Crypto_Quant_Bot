
import sys
import os
import time
import logging

# Setup basic logging to capture output
logging.basicConfig(level=logging.INFO)

sys.path.append(os.getcwd())

# Mock Config to prevent full system load? 
# No, we want integration test.

try:
    from engine.runner import BotRunner
    
    print("--- SIMULATING RUNNER STARTUP (AUTO-HEALING TEST) ---")
    
    # Initialize Runner (This triggers __init__ -> startup_sync)
    # Be careful not to start the loop
    runner = BotRunner()
    
    print("✅ Runner Initialized (Startup Sync should have run).")
    
    # Now verify if the ghost order is gone
    from engine.exchange_interface import ExchangeInterface
    ex = ExchangeInterface(market_type='future')
    orders = ex.fetch_open_orders()
    
    if not orders:
        print("✅ SUCCESS: No open orders found! Auto-healing worked.")
    else:
        print(f"❌ FAILURE: Found {len(orders)} open orders. Auto-healing failed.")
        for o in orders:
            print(f"   - {o['id']} ({o['symbol']})")

except Exception as e:
    print(f"❌ TEST FAILED with Error: {e}")
