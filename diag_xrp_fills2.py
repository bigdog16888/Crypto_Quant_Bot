import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

# Get the last TP time
c.execute("SELECT timestamp FROM trade_history WHERE bot_id=10017 AND action='TP_HIT' ORDER BY timestamp DESC LIMIT 2")
tps = c.fetchall()
last_tp = tps[0][0] if len(tps) > 0 else 1773025367
prev_tp = tps[1][0] if len(tps) > 1 else 0

print(f"Analyzing cycle between {prev_tp} and {last_tp}")

# Get all filled grid/entry orders in that window
c.execute("SELECT order_type, amount, price, status, created_at, client_order_id FROM bot_orders WHERE bot_id=10017 AND created_at > ? AND created_at <= ? ORDER BY created_at ASC", (prev_tp, last_tp))
orders = c.fetchall()

total_qty = 0
total_cost = 0

print("Fills in cycle:")
for o in orders:
    # check for filled grids/entries or WS_ENTRY_PARTIAL in history?
    # bot_orders might have 'filled' status or status=amount
    if o[0] in ('entry', 'grid') and (o[3] == 'filled' or str(o[3]) == str(o[1])):
        try:
             amt = float(o[1])
             prc = float(o[2])
             total_qty += amt
             total_cost += (amt * prc)
             print(f"  + Fill: {amt} @ {prc} (cid: {o[5]}) time: {o[4]}")
        except: pass

print("-" * 40)
if total_qty > 0:
    avg = total_cost / total_qty
    print(f"Calculated True DB Avg Entry: {avg:.4f}")
    print(f"Calculated True DB Qty: {total_qty:.2f}")
    
    tp_price = 1.3470
    pnl = (tp_price - avg) * total_qty
    print(f"If TP at 1.3470 -> True PNL: {pnl:.2f}")

    db_pnl = 94.22
    db_avg = tp_price - (db_pnl / total_qty)
    print(f"Implied DB Avg Entry (based on $94 PNL): {db_avg:.4f}")

else:
    print("No fills found")

conn.close()
