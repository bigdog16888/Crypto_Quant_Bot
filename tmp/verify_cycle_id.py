import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Verify exactly what cycle_id the PASS3 adoption row has
c.execute("SELECT id, cycle_id, client_order_id, filled_amount, status FROM bot_orders WHERE bot_id=10018 AND order_type='adoption'")
for r in c.fetchall():
    print("adoption:", r)

# Also show the current trades row
c.execute("SELECT cycle_id FROM trades WHERE bot_id=10018")
print("trades.cycle_id:", c.fetchone())

conn.close()
