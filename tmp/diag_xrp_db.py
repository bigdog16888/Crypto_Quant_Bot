import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("=== XRP BOTS ===")
c.execute("SELECT id, name, pair, direction FROM bots WHERE pair LIKE '%XRP%'")
for r in c.fetchall():
    print(r)

print("\n=== XRP bot_orders (cycle 7) ===")
# Bot 10017 is cycle 7
c.execute("""
    SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id, order_id
    FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=7
    ORDER BY created_at ASC
""")
for r in c.fetchall():
    print(r)

conn.close()
