import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
c = conn.cursor()

c.execute("SELECT cycle_id, total_invested, avg_entry_price FROM trades WHERE bot_id=10018")
trade = c.fetchone()
cycle_id, current_total, current_avg = trade[0], trade[1], trade[2]
print(f"Bot 10018 (SUI) — cycle_id={cycle_id}")
print(f"Current DB total_invested: ${current_total:.2f}")

# Show all filled entry/grid orders
c.execute("""
    SELECT client_order_id, order_type, amount, price, status
    FROM bot_orders
    WHERE bot_id=10018 AND cycle_id=? AND order_type IN ('entry','grid') AND status='filled'
    ORDER BY created_at
""", (cycle_id,))
rows = c.fetchall()
total = 0.0
qty = 0.0
print("\nFilled entry/grid orders:")
for r in rows:
    cost = (r[2] or 0) * (r[3] or 0)
    total += cost
    qty += (r[2] or 0)
    flag = " ← GAP_REPAIR" if 'GAP_REPAIR' in (r[0] or '') or 'OFFLINE_FILL' in (r[0] or '') else ""
    print(f"  {r[1]:6} {r[0]:50} ${cost:.2f}{flag}")

print(f"\nSum of real fills: ${total:.2f}")
print(f"Exchange shows:    $803.63")
print(f"Excess:            ${total - 803.63:.2f}")

# Check for GAP_REPAIR entries to delete
c.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=10018 AND (order_id LIKE 'GAP_REPAIR%' OR client_order_id LIKE '%REPAIR%')")
repair_count = c.fetchone()[0]
print(f"\nGAP_REPAIR entries to remove: {repair_count}")
conn.close()
