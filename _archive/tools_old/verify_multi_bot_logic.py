import logging

# Mock logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Test")

def verify_state_sync_logic(bot_id, name, open_orders_snapshot):
    print(f"\nTesting Bot {bot_id} ({name})...")
    
    bot_prefix = f"CQB_{bot_id}_"
    my_tp_orders = [o for o in open_orders_snapshot 
                   if o.get('clientOrderId', '').startswith(f'{bot_prefix}TP_')]
    my_grid_orders = [o for o in open_orders_snapshot 
                     if o.get('clientOrderId', '').startswith(f'{bot_prefix}GRID_')]
    has_my_orders = len(my_tp_orders) + len(my_grid_orders) > 0
    
    print(f"  Prefix: {bot_prefix}")
    print(f"  My TP Orders: {[o['clientOrderId'] for o in my_tp_orders]}")
    print(f"  My Grid Orders: {[o['clientOrderId'] for o in my_grid_orders]}")
    print(f"  Has My Orders: {has_my_orders}")
    
    if has_my_orders:
        print("  ✅ Result: Bot is SAFE (Orders found)")
        return True
    else:
        print("  ❌ Result: Bot is GHOST (No orders found)")
        return False

def run_test():
    # Scenario: 3 Bots Active
    # Bot A (10000)
    # Bot B (10001)
    # Bot C (10002)
    
    # Mock Open Orders on Exchange (All mixed together)
    open_orders = [
        {'clientOrderId': 'CQB_10000_TP_0', 'symbol': 'BTC/USDC'},
        {'clientOrderId': 'CQB_10000_GRID_1', 'symbol': 'BTC/USDC'},
        {'clientOrderId': 'CQB_10001_TP_0', 'symbol': 'BTC/USDC'},
        {'clientOrderId': 'CQB_10002_TP_0', 'symbol': 'BTC/USDC'},
        {'clientOrderId': 'CQB_10002_GRID_1', 'symbol': 'BTC/USDC'},
        {'clientOrderId': 'CQB_10002_GRID_2', 'symbol': 'BTC/USDC'},
    ]
    
    print("--- Test 1: All Bots Have Orders ---")
    res_a = verify_state_sync_logic(10000, "Bot A", open_orders)
    res_b = verify_state_sync_logic(10001, "Bot B", open_orders)
    res_c = verify_state_sync_logic(10002, "Bot C", open_orders)
    
    if res_a and res_b and res_c:
        print("\n✅ SUCCESS: All bots detected their orders correctly.")
    else:
        print("\n❌ FAILURE: Some bots failed detection.")

    print("\n--- Test 2: Bot A Lost Orders (Ghost) ---")
    # Remove Bot A's orders
    orders_no_a = [o for o in open_orders if 'CQB_10000_' not in o['clientOrderId']]
    
    res_a = verify_state_sync_logic(10000, "Bot A", orders_no_a)
    res_b = verify_state_sync_logic(10001, "Bot B", orders_no_a)
    
    if not res_a and res_b:
        print("\n✅ SUCCESS: Bot A detected as Ghost, Bot B still safe.")
    else:
        print("\n❌ FAILURE: Logic incorrect for Ghost scenario.")

if __name__ == "__main__":
    run_test()
