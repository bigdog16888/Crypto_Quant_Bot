import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
cycle = c.fetchone()[0]

print(f"=== XRP bot_orders (cycle {cycle}) ===")
c.execute("""
    SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id, order_id
    FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=?
    ORDER BY created_at ASC
""", (cycle,))
for r in c.fetchall():
    print(r)

conn.close()
