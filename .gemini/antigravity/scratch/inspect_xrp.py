import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("--- Trades Table (10017) ---")
cur.execute("SELECT cycle_id, open_qty, total_invested, cycle_phase FROM trades WHERE bot_id=10017")
row = cur.fetchone()
if row:
    print(dict(row))

print("\n--- Recent Filled Orders (10017) ---")
cur.execute("""
    SELECT id, order_id, order_type, filled_amount, price, status, cycle_id, created_at 
    FROM bot_orders 
    WHERE bot_id=10017 AND status='filled' 
    ORDER BY id DESC LIMIT 10
""")
for r in cur.fetchall():
    print(dict(r))

print("\n--- Current Open Orders in DB (10017) ---")
cur.execute("""
    SELECT id, order_id, order_type, price, status, cycle_id
    FROM bot_orders 
    WHERE bot_id=10017 AND status='open'
""")
for r in cur.fetchall():
    print(dict(r))

conn.close()
