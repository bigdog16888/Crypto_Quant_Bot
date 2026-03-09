import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

# Mark the weekend offline fill as filled
c.execute("UPDATE bot_orders SET status='filled', updated_at=1773016000 WHERE client_order_id='CQB_10017_GRID_2_1772784150'")
print(f'Updated GRID_2 offline fill rows: {c.rowcount}')
conn.commit()

# Recompute total_invested
c.execute("SELECT cycle_id FROM trades WHERE bot_id=10017")
cycle_id = c.fetchone()[0]
c.execute("""
    SELECT SUM(amount * price), SUM(amount) FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=? AND status='filled' AND order_type IN ('entry','grid')
""", (cycle_id,))
r = c.fetchone()
total = r[0] or 0.0
qty = r[1] or 0.0
avg = total / qty if qty else 0

c.execute('UPDATE trades SET total_invested=?, avg_entry_price=? WHERE bot_id=10017', (total, avg))
conn.commit()
print(f'Recomputed: total_invested=${total:.2f}, avg_entry_price={avg:.6f}, total_qty={qty:.4f}')
conn.close()
