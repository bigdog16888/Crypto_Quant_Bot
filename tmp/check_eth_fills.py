import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT id, status, order_type, amount, filled_amount, created_at, updated_at, client_order_id, cycle_id FROM bot_orders WHERE bot_id=10011 AND filled_amount > 0 ORDER BY updated_at DESC LIMIT 20")
print('ID | STATUS | TYPE | AMOUNT | FILLED | CREATED | UPDATED | CID | CYCLE')
for r in q.fetchall():
    print(r)
c.close()
