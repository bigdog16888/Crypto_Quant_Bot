import sqlite3

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT order_type, amount, filled_amount, status, created_at, cycle_id, notes FROM bot_orders WHERE bot_id=10022 ORDER BY created_at DESC LIMIT 20")
print("RECENT BOT_ORDERS FOR BTC:")
for o in c.fetchall():
    print(f"  {o['created_at']}: type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']} cycle={o['cycle_id']} notes={str(o['notes'])[:50]}")

c.execute("SELECT current_step, open_qty, cycle_id, cycle_phase FROM trades WHERE bot_id=10022")
print("\nTRADES:", dict(c.fetchone()))

conn.close()
