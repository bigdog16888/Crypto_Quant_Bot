import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== EXACT recompute arithmetic for short_eth (100002) cycle=13 ===")
# Replicate recompute_invested_from_orders SQL exactly
q.execute("""
    SELECT 
        bo.order_type, bo.status, bo.filled_amount, bo.price, bo.cycle_id,
        bo.client_order_id,
        CASE 
            WHEN bo.order_type IN ('entry','grid','adoption_add','adoption') THEN (bo.filled_amount * bo.price)
            WHEN bo.order_type IN ('adoption_reduce','tp','close','dust_close','sl') THEN -(bo.filled_amount * bo.price)
            ELSE 0
        END as cost_contribution
    FROM bot_orders bo
    WHERE bo.bot_id = 100002
      AND bo.cycle_id = 13
      AND bo.filled_amount > 0
      AND bo.price > 0
      AND bo.client_order_id LIKE 'CQB_%'
      AND bo.status NOT IN ('placing','failed','auto_closed')
    ORDER BY bo.created_at
""")
rows = q.fetchall()
print(f"  {'Type':<15} {'Status':<15} {'Qty':>8} {'Price':>10} {'CID':<40} {'Cost':>10}")
print(f"  {'-'*100}")
total = 0
for r in rows:
    print(f"  {r[0]:<15} {r[1]:<15} {r[2]:>8.4f} {r[3]:>10.2f} {r[5][:40]:<40} {r[6]:>10.2f}")
    total += r[6]
print(f"  {'TOTAL':>80} {total:>10.2f}")
print(f"  Implied qty = {total / 2038}") 

print()
print("=== PROBLEM: same-cycle reset_cleared fills are double-counted ===")
reset_fills = [(r[0], r[1], r[2], r[3]) for r in rows if r[1] == 'reset_cleared']
active_fills = [(r[0], r[1], r[2], r[3]) for r in rows if r[1] not in ('reset_cleared',)]
print(f"  Active fills: {active_fills}")
print(f"  Reset_cleared fills counted: {reset_fills}")
print()
print(f"  Active fills total cost: {sum(r[2]*r[3] for r in active_fills):.2f}")
print(f"  Reset_cleared 'extra' cost: {sum(r[2]*r[3] for r in reset_fills):.2f}")
print(f"  actual exchange fills (entry+grid without adoption): {sum(r[2] for r in active_fills if r[0] not in ('adoption',)):.4f} ETH")

c.close()
