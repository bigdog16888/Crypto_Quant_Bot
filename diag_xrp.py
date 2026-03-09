import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

# Show all filled entry/grid orders in detail to find the excess $63
c.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
cycle_id = c.fetchone()[0]

c.execute("""
    SELECT client_order_id, order_type, amount, price, status, notes
    FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=? AND status='filled' AND order_type IN ('entry','grid')
    ORDER BY created_at
""", (cycle_id,))
rows = c.fetchall()
total = 0.0
for r in rows:
    cost = (r[2] or 0) * (r[3] or 0)
    total += cost
    print(f"  {r[1]:6} {r[0]:45} cost=${cost:.2f}")
print(f"\nTotal: ${total:.2f}  (exchange shows $1069.75)")
print(f"Excess: ${total - 1069.75:.2f}")
conn.close()
