import sqlite3
import sys
import os

# Set up path to import engine packages if needed
sys.path.append(os.path.abspath('.'))

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("--- 1. bot_orders with filled_amount > 0 for bot 100317 ---")
    rows = cursor.execute("""
        SELECT cycle_id, order_type, status, filled_amount, price, client_order_id
        FROM bot_orders 
        WHERE bot_id = 100317 AND filled_amount > 0
        ORDER BY created_at ASC
    """).fetchall()
    for r in rows:
        print(r)
        
    print("\n--- 2. Checking CQB_100317_TP_19_BE_FB status in bot_orders ---")
    tp_rows = cursor.execute("""
        SELECT cycle_id, order_type, status, amount, price, client_order_id
        FROM bot_orders
        WHERE bot_id = 100317 AND client_order_id = 'CQB_100317_TP_19_BE_FB'
    """).fetchall()
    for r in tp_rows:
        print(r)

    print("\n--- 3. Running seal_trade_state(100317) ---")
    from engine.ledger import seal_trade_state
    res = seal_trade_state(100317)
    print(f"Result from seal_trade_state: {res}")

    print("\n--- 4. Checking trades row for bot 100317 ---")
    trade_row = cursor.execute("""
        SELECT open_qty, avg_entry_price, cycle_id, tp_order_id
        FROM trades
        WHERE bot_id = 100317
    """).fetchone()
    print(f"trades row: open_qty={trade_row[0]}, avg_entry_price={trade_row[1]}, cycle_id={trade_row[2]}, tp_order_id={trade_row[3]}")

if __name__ == '__main__':
    run()
