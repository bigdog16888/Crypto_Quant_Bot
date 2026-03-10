import sqlite3
import json

conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("""
    SELECT 
        datetime(created_at, 'unixepoch', 'localtime') as local_time, 
        order_type, 
        price, 
        amount, 
        status, 
        client_order_id, 
        notes, 
        updated_at,
        cycle_id
    FROM bot_orders 
    WHERE bot_id=10022 AND status IN ('filled', 'closed', 'canceled', 'open', 'reset_cleared') 
    ORDER BY created_at ASC
""")
rows = c.fetchall()

total_amount = 0.0
print("--- ALL HISTORICAL ORDERS WRITTEN TO orders_full.json ---")
out = []
for r in rows:
    d = dict(r)
    # Calculate sum if filled
    if d['status'] in ('filled', 'closed'):
        if d['order_type'] in ('entry', 'grid'):
             total_amount += float(d['amount'])
        elif d['order_type'] == 'tp':
             # Reset on TP
             total_amount = 0.0
             
    d['running_amount_approx'] = total_amount
    out.append(d)

with open('orders_full.json', 'w') as f:
    json.dump(out, f, indent=2)

print("wrote orders_full.json")
