import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Check if the new grid fill 1773014897699 (timestamp from CCXT) is in bot_orders by client order ID
c.execute("SELECT order_id, client_order_id, status, amount, price FROM bot_orders WHERE bot_id=10017 AND client_order_id LIKE '%1773014897%'")
print('New GRID_2 in DB (by CID match):', c.fetchall())

# All orders in current cycle
c.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
cycle_id = c.fetchone()[0]
c.execute("SELECT order_type, order_id, client_order_id, status, amount, price FROM bot_orders WHERE bot_id=10017 AND cycle_id=? ORDER BY created_at DESC", (cycle_id,))
print('\nAll orders for bot 10017 current cycle:')
for r in c.fetchall():
    print(' ', r)

conn.close()
