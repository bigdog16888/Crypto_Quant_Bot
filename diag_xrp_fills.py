import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("SELECT cycle_id, timestamp FROM trade_history WHERE bot_id=10017 AND action='BASKET_START' ORDER BY timestamp DESC LIMIT 2")
starts = c.fetchall()
start_t = starts[1][1] if len(starts) > 1 else 0
end_t = starts[0][1] if len(starts) > 0 else 1773025367

print(f"Cycle Start: {start_t}, TP Exit Time: {end_t}")

c.execute("SELECT order_type, amount, price, status, created_at, client_order_id FROM bot_orders WHERE bot_id=10017 AND created_at >= ? AND created_at <= ? ORDER BY created_at ASC", (start_t, end_t + 10))
orders = c.fetchall()

total_qty = 0
total_cost = 0

print("Fills in cycle:")
for o in orders:
    # Only filled entries/grids
    if o[0] in ('entry', 'grid') and o[3] in ('filled', str(o[1]), o[1]):  # The UI sometimes marks status as the amount
        try:
             amt = float(o[1])
             prc = float(o[2])
             total_qty += amt
             total_cost += (amt * prc)
             print(f"  + Fill: {amt} @ {prc} (cid: {o[5]})")
        except: pass

print("-" * 40)
if total_qty > 0:
    avg = total_cost / total_qty
    print(f"Calculated DB Avg Entry: {avg:.4f}")
    print(f"Calculated DB Qty: {total_qty:.2f}")
    
    tp_price = 1.3470
    pnl = (tp_price - avg) * total_qty
    print(f"If TP at 1.3470 -> PNL: {pnl:.2f}")
else:
    print("No fills found")

c.execute("SELECT * FROM trade_history WHERE bot_id=10017 AND action='TP_HIT' ORDER BY timestamp DESC LIMIT 1")
print("\nDB TP_HIT record:", c.fetchone())

conn.close()
