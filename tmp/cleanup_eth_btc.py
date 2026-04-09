import sqlite3
import time

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== CLEANUP: Remove stale phys_qty adoption for short_eth (100002) ===")
# This adoption was written with phys_qty=0.043 when gap was only 0.031 (phys=0.043 - entry=0.012)
# Now that recompute excludes reset_cleared, the adoption qty should only cover the remaining gap
# The next reconciler run will compute the correct gap and write a new adoption if needed.
q.execute("""
    DELETE FROM bot_orders 
    WHERE bot_id=100002 
    AND order_type='adoption' 
    AND status='filled'
    AND client_order_id LIKE '%PASS3%'
""")
print(f"  Deleted {q.rowcount} stale PASS-3 adoption(s)")

print()
print("=== WHAT recompute WILL SEE after cleanup ===")
q.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id
    FROM bot_orders WHERE bot_id=100002
    AND cycle_id=13
    AND filled_amount>0 AND price>0
    AND client_order_id LIKE 'CQB_%'
    AND status NOT IN ('placing','failed','auto_closed','reset_cleared')
    ORDER BY created_at
""")
rows = q.fetchall()
total_cost = 0
total_qty = 0
for r in rows:
    cost = r[2] * r[3]
    print(f"  {r[0]:<15} {r[1]:<10} qty={r[2]:.4f} price={r[3]:.2f} → ${cost:.2f}")
    if r[0] in ('entry','grid','adoption_add','adoption'):
        total_cost += cost
        total_qty += r[2]
    elif r[0] in ('tp','close','adoption_reduce'):
        total_cost -= cost
        total_qty -= r[2]

print(f"  ─────────────────────────────────────────────────────────")
print(f"  Recompute will yield: total_invested=${total_cost:.2f}, qty={total_qty:.4f}")
print(f"  Physical on exchange: 0.1430 ETH")
print(f"  Gap (to be covered by next PASS-3): {0.143 - total_qty:.4f} ETH")

print()
print("=== BTC: active_positions has bot_id=0 (orphaned) ===")
q.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='BTCUSDC'")
r = q.fetchone()
if r:
    print(f"  Current: bot_id={r[0]}, pair={r[1]}, side={r[2]}, size={r[3]}")
    print(f"  Problem: bot_id=0 means no bot owns this BTC position → system NET = $0")
    print(f"  long btc price (10016) should own cycle=10 but trades.total_invested=0.0")
    print(f"  The reconciler PASS-3 would normally adopt this — but long_btc_price is in scanning state")
    print(f"  with total_invested=0, so active_positions.bot_id=0 and the adoption order also has bot_id=10016")
    q.execute("SELECT order_type, status, filled_amount, cycle_id FROM bot_orders WHERE bot_id=10016 AND filled_amount>0 ORDER BY created_at DESC LIMIT 5")
    for row in q.fetchall(): print(f"    {row}")
else:
    print("  No BTC in active_positions")

c.commit()
c.close()
print()
print("Done. Restart the engine for both fixes to take effect.")
