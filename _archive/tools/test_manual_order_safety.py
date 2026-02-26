
import sys
import os
import time
import logging

# Setup basic logging to capture output
logging.basicConfig(level=logging.INFO)

sys.path.append(os.getcwd())

from config.settings import config

try:
    print(f"--- SAFETY TEST: STRICT_CLEANUP is {config.STRICT_CLEANUP} ---")
    if config.STRICT_CLEANUP:
        print("⚠️ WARNING: Strict Cleanup is ON. Manual orders WILL be deleted!")
    else:
        print("✅ GOOD: Strict Cleanup is OFF. Manual orders should be safe.")

    from engine.runner import BotRunner
    from engine.exchange_interface import ExchangeInterface
    
    ex = ExchangeInterface(market_type='future')
    
    # 1. Create a MANUAL Order (No 'CQB_' prefix)
    symbol = 'XAU/USDT'
    price = 1400.0 # Way below market
    qty = 0.01 
    cid = f"MANUAL_TEST_{int(time.time())}"
    
    print(f"1. Placing MANUAL order {cid}...")
    res = ex.create_order(symbol, 'LIMIT', 'BUY', qty, price, params={'clientOrderId': cid})
    order_id = res['id']
    print(f"✅ Created Order: {order_id}")
    
    # 2. Run Startup Sync
    print("2. Running Runner Startup Sync...")
    runner = BotRunner()
    
    # 3. Verify Order Still Exists
    print("3. Verifying Order Survival...")
    orders = ex.fetch_open_orders()
    found = False
    for o in orders:
        if str(o['id']) == str(order_id):
            found = True
            break
            
    if found:
        print(f"✅ SUCCESS: Manual Order {order_id} SURVIVED!")
        # Cleanup
        print("4. Cleaning up test order...")
        ex.cancel_order(order_id, symbol)
    else:
        print(f"❌ FAILURE: Manual Order {order_id} was DELETED!")

except Exception as e:
    print(f"❌ TEST FAILED with Error: {e}")
