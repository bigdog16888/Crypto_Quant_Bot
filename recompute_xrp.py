import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("""SELECT client_order_id, order_type, amount, price, status 
             FROM bot_orders 
             WHERE bot_id=10017 AND order_type IN ('entry','grid') AND status='filled' 
             ORDER BY created_at""")
rows = c.fetchall()
total = 0.0
total_qty = 0.0
for r in rows:
    cost = (r[2] or 0) * (r[3] or 0)
    total += cost
    total_qty += (r[2] or 0)
    print(r[0], r[1], r[4], f"qty={r[2]:.4f}", f"price={r[3]:.5f}", f"cost={cost:.2f}")

avg = total / total_qty if total_qty else 0
print(f"\nTotal invested from filled orders: ${total:.4f}")
print(f"Total qty: {total_qty:.4f}")
print(f"Computed avg_entry_price: {avg:.6f}")

c.execute("UPDATE trades SET total_invested=?, avg_entry_price=? WHERE bot_id=10017", (total, avg))
conn.commit()
print(f"\nUpdated trades.total_invested to ${total:.2f}")
conn.close()
