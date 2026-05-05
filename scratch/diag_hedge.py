import sqlite3

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT id, order_type, amount, filled_amount, status, created_at, notes FROM bot_orders WHERE bot_id=10022 AND order_type='hedge' ORDER BY created_at ASC")
print("ALL HEDGE ORDERS:")
for o in c.fetchall():
    print(f"  {o['created_at']}: amt={o['amount']} fill={o['filled_amount']} status={o['status']} notes={o['notes']}")

c.execute("SELECT id, order_type, amount, filled_amount, status, created_at, notes FROM bot_orders WHERE bot_id=10022 AND order_type IN ('entry', 'grid') ORDER BY created_at ASC")
print("\nALL ENTRY/GRID ORDERS:")
for o in c.fetchall():
    if o['filled_amount'] > 0:
        print(f"  {o['created_at']}: type={o['order_type']} fill={o['filled_amount']}")

conn.close()
