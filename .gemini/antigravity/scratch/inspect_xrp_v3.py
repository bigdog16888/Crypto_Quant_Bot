import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("--- Cycle 6 Fills (10017) ---")
cur.execute("""
    SELECT order_id, order_type, filled_amount, status, cycle_id 
    FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=6 AND status='filled'
""")
for r in cur.fetchall():
    print(dict(r))

print("\n--- Cycle 6 All Orders (10017) ---")
cur.execute("""
    SELECT id, order_id, order_type, filled_amount, status, cycle_id 
    FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=6
""")
for r in cur.fetchall():
    print(dict(r))

conn.close()
