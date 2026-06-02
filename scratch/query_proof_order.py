import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM bot_orders WHERE order_id = '96660351' OR client_order_id LIKE '%96660351%'")
    rows = cur.fetchall()
    cur.execute("PRAGMA table_info(bot_orders)")
    cols = [c[1] for c in cur.fetchall()]
    print("--- Matching Bot Orders ---")
    for r in rows:
        for col, val in zip(cols, r):
            print(f"  {col}: {val}")
            
    # Also search for SUI order fills in trade_history or bot_orders
    print("\n--- Recent SUI Bot Orders ---")
    cur.execute("SELECT id, order_id, order_type, price, amount, filled_amount, status, step, cycle_id, created_at FROM bot_orders WHERE bot_id = 10018 ORDER BY created_at DESC LIMIT 15")
    rows_sui = cur.fetchall()
    for o in rows_sui:
         print(f"  ID: {o[0]} | OID: {o[1]} | Type: {o[2]} | Price: {o[3]} | Qty: {o[4]} | Filled: {o[5]} | Status: {o[6]} | Step: {o[7]} | CycleID: {o[8]}")
         
    conn.close()

if __name__ == '__main__':
    run()
