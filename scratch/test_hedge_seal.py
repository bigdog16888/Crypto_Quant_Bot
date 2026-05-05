import os
import sys
import time

# Add the project root to sys.path
project_root = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot'
if project_root not in sys.path:
    sys.path.append(project_root)

from engine.database import get_connection, sync_trades_from_orders
from engine.ledger import seal_trade_state

def test_hedge_seal():
    bot_id = 99999
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Setup mock bot and trades
    cursor.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    cursor.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
    cursor.execute("DELETE FROM bot_orders WHERE bot_id=?", (bot_id,))
    
    cursor.execute("""
        INSERT INTO bots (id, name, pair, direction, is_active, status)
        VALUES (?, 'HedgeTest', 'BTCUSDC', 'LONG', 1, 'IN TRADE')
    """, (bot_id,))
    
    cursor.execute("""
        INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, cycle_id, position_side)
        VALUES (?, 1000, 50000, 1, 1, 'LONG')
    """, (bot_id,))
    
    # 2. Add orders: 1 Entry (0.02 BTC), 1 Hedge (0.01 BTC), 1 partial TP (0.01 BTC)
    # This results in total_qty = 0.02 - 0.01 = 0.01
    # hedge_qty = 0.01
    # total_net_qty = 0.01 - 0.01 = 0.0
    
    now = int(time.time())
    
    # Entry
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, created_at, client_order_id)
        VALUES (?, 'entry', 'E1', 50000, 0.02, 0.02, 'filled', 1, ?, 'CQB_99999_E1')
    """, (bot_id, now - 100))
    
    # Hedge
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, created_at, client_order_id)
        VALUES (?, 'hedge_open', 'H1', 50000, 0.01, 0.01, 'filled', 1, ?, 'CQB_99999_H1')
    """, (bot_id, now - 50))
    
    # Partial TP
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, created_at, client_order_id)
        VALUES (?, 'tp', 'T1', 55000, 0.01, 0.01, 'filled', 1, ?, 'CQB_99999_T1')
    """, (bot_id, now - 10))
    
    conn.commit()
    
    print(f"--- INITIAL STATE ---")
    res = cursor.execute("SELECT total_invested, entry_confirmed, cycle_phase FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    print(f"Invested: {res[0]}, Confirmed: {res[1]}, Phase: {res[2]}")
    
    # 3. Run seal_trade_state
    print(f"\n--- RUNNING seal_trade_state ---")
    seal_trade_state(bot_id)
    
    res = cursor.execute("SELECT total_invested, entry_confirmed, cycle_phase FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    print(f"Invested: {res[0]}, Confirmed: {res[1]}, Phase: {res[2]}")
    
    if res[1] == 1:
        print("✅ SUCCESS: entry_confirmed PRESERVED in 1:1 hedge state.")
    else:
        print("❌ FAILURE: entry_confirmed RESET to 0 in 1:1 hedge state.")

    # 4. Run sync_trades_from_orders
    print(f"\n--- RUNNING sync_trades_from_orders ---")
    sync_trades_from_orders(bot_id)
    
    res = cursor.execute("SELECT total_invested, entry_confirmed, cycle_phase FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    print(f"Invested: {res[0]}, Confirmed: {res[1]}, Phase: {res[2]}")

    if res[1] == 1:
        print("✅ SUCCESS: entry_confirmed PRESERVED after sync.")
    else:
        print("❌ FAILURE: entry_confirmed RESET after sync.")

    # Cleanup
    cursor.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    cursor.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
    cursor.execute("DELETE FROM bot_orders WHERE bot_id=?", (bot_id,))
    conn.commit()

if __name__ == '__main__':
    test_hedge_seal()
