import sys
import time
import sqlite3
sys.path.insert(0, '.')

from engine.database import get_pair_virtual_net
from engine.ledger import seal_trade_state

DB_PATH = 'crypto_bot.db'

def run_fix():
    print("=== BEFORE FIX ===")
    vnet_before = get_pair_virtual_net('ETH/USDC:USDC')
    print(f"Virtual Net: {vnet_before}")
    
    # Check open_qty of bot 100002 before fix
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    trade_before = cursor.execute(
        "SELECT open_qty, total_invested, avg_entry_price, cycle_id, entry_confirmed "
        "FROM trades WHERE bot_id = 100002"
    ).fetchone()
    print(f"Bot 100002 trades record before fix: {trade_before}")
    
    # Inject missing record
    print("\nInjecting missing exit record...")
    now_ts = int(time.time())
    insert_query = """
    INSERT INTO bot_orders (
        bot_id, order_type, status, filled_amount, amount, price,
        cycle_id, step, position_side, client_order_id, created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        100002, 'close', 'filled', 0.066, 0.066, 2016.37,
        33, 1, 'SHORT', 'MANUAL_CLOSE_100002_33', now_ts, now_ts
    )
    
    try:
        cursor.execute(insert_query, params)
        conn.commit()
        print("Record successfully injected!")
    except Exception as e:
        print(f"Error during INSERT: {e}")
        conn.close()
        return
        
    conn.close()
    
    # Call seal
    print("\nSealing trade state for bot 100002...")
    seal_res = seal_trade_state(100002)
    print(f"Seal result: {seal_res}")
    
    # Verify after fix
    print("\n=== AFTER FIX ===")
    vnet_after = get_pair_virtual_net('ETH/USDC:USDC')
    print(f"Virtual Net: {vnet_after}")
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    trade_after = cursor.execute(
        "SELECT open_qty, total_invested, avg_entry_price, cycle_id, entry_confirmed "
        "FROM trades WHERE bot_id = 100002"
    ).fetchone()
    print(f"Bot 100002 trades record after fix: {trade_after}")
    
    bot_status = cursor.execute(
        "SELECT status, is_active FROM bots WHERE id = 100002"
    ).fetchone()
    print(f"Bot 100002 bots record after fix: {bot_status}")
    conn.close()

if __name__ == "__main__":
    run_fix()
