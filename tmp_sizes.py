import sqlite3
import json

conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT step, order_type, amount, price, status, created_at, client_order_id FROM bot_orders WHERE bot_id=10022 AND order_type IN ('entry', 'grid') ORDER BY created_at ASC")
rows = c.fetchall()

out = []
running = 0.0
for r in rows:
    if float(r['amount']) > 0:
        qty = float(r['amount'])
        if r['status'] in ('filled', 'closed'):
            running += qty
        elif r['status'] == 'reset_cleared':
            pass
        out.append(f"Step {r['step']} | Amt: {qty} | Type: {r['order_type']} | Status: {r['status']} | Total: {running} | ID: {r['client_order_id']}")

with open('sizes.txt', 'w') as f:
    f.write('\n'.join(out))
