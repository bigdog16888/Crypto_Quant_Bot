import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

# ==== SOL DEEP DIVE ====
print("=== SOL: long sol (10008) cycle=12 ALL bot_orders ===")
q.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10008 
    AND cycle_id=12 AND filled_amount>0
    ORDER BY created_at DESC LIMIT 15
""")
for r in q.fetchall(): print(f"  {r[0]:<15} {r[1]:<15} qty={r[2]:.4f} price={r[3]:.4f} cid={r[5][:50]}")

print("\n=== SOL: Why recompute sees $10/0.12 for cycle=12 ===")
q.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id, client_order_id,
    CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount*price
         WHEN order_type IN ('adoption_reduce','tp','close','dust_close','sl') THEN -filled_amount*price
         ELSE 0 END as cost
    FROM bot_orders WHERE bot_id=10008 AND cycle_id=12
    AND filled_amount>0 AND price>0 AND client_order_id LIKE 'CQB_%'
    AND status NOT IN ('placing','failed','auto_closed','reset_cleared')
    ORDER BY created_at
""")
rows = q.fetchall()
print(f"  Count: {len(rows)} rows that recompute sees")
for r in rows: print(f"  {r[0]:<12} {r[1]:<12} qty={r[2]:.4f} price={r[3]:.4f} cost=${r[6]:.2f} [{r[5][:50]}]")

print("\n=== SOL: active_positions ===")
q.execute("SELECT * FROM active_positions WHERE pair='SOLUSDC'")
for r in q.fetchall(): print(f"  {r}")

print("\n=== SOL: What does PASS-3 oid look like? ===")
q.execute("SELECT * FROM bot_orders WHERE bot_id=10008 AND order_type='adoption' AND status='filled' ORDER BY created_at DESC LIMIT 5")
for r in q.fetchall(): print(f"  {r}")

# ==== BTC GAP INVESTIGATION ====
print("\n\n=== BTC: Why PASS-3 wrote 0.017 instead of 0.023 ===")
print("Current cycle=10. PASS-3 adoption=0.017. Physical=0.023.")
print("Gap = 0.023 - true_qty. So true_qty was 0.006 when PASS-3 ran.")
print("What gives true_qty=0.006 for cycle=10?")
q.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=10
    AND filled_amount>0 AND price>0 AND client_order_id LIKE 'CQB_%'
    AND status NOT IN ('placing','failed','auto_closed')
    ORDER BY created_at
""")
rows = q.fetchall()
print(f"  All cycle=10 orders (incl reset_cleared):")
for r in rows: print(f"    {r[0]:<12} {r[1]:<12} qty={r[2]:.4f} cid={r[5][:60]}")

print("\n=== BTC cycle=10 with OLD recompute (including reset_cleared) ===")
q.execute("""
    SELECT SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END) as qty,
           COUNT(*) as cnt
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=10
    AND filled_amount>0 AND price>0 AND client_order_id LIKE 'CQB_%'
    AND status NOT IN ('placing','failed','auto_closed')
""")
r = q.fetchone()
print(f"  Old recompute (incl reset_cleared): qty={r[0]:.4f}, count={r[1]}")

# ==== XRP GAP  ====
print("\n\n=== XRP: 3.7 XRP gap — new grid fills since PASS-3 ran? ===")
q.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id, created_at
    FROM bot_orders WHERE bot_id=10017 AND cycle_id=42
    AND filled_amount>0
    ORDER BY created_at DESC LIMIT 10
""")
for r in q.fetchall(): print(f"  {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} ts={r[5]}")

c.close()
