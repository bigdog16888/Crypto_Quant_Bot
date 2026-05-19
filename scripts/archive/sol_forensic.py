import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

print("=" * 70)
print("PART 1: Bot 100001 reset_cleared rows for cycle 26 with filled > 0")
print("=" * 70)
cursor.execute("""
SELECT order_id, client_order_id, order_type, filled_amount, price, 
       status, cycle_id, created_at, wipe_proof_source
FROM bot_orders
WHERE bot_id = 100001
  AND status = 'reset_cleared'
  AND cycle_id = 26
  AND filled_amount > 0
ORDER BY created_at ASC
""")
rows = cursor.fetchall()
for r in rows:
    print(f"  order_id={r[0]} | cid={r[1]} | type={r[2]} | filled={r[3]} "
          f"| price={r[4]} | status={r[5]} | cycle={r[6]} | proof={r[8]}")
print(f"  => {len(rows)} rows found\n")

print("=" * 70)
print("PART 2: Bot 100001 cycle 26 — ALL orders with filled > 0 by type")
print("=" * 70)
cursor.execute("""
SELECT order_type, SUM(filled_amount) as total_filled, COUNT(*) as rows, 
       GROUP_CONCAT(status) as statuses
FROM bot_orders
WHERE bot_id = 100001
  AND cycle_id = 26
  AND filled_amount > 0
GROUP BY order_type
ORDER BY order_type
""")
for r in cursor.fetchall():
    print(f"  type={r[0]:20} | total_filled={r[1]:.4f} | rows={r[2]} | statuses={r[3]}")

print("\n" + "=" * 70)
print("PART 3: trades row for bot 100001")
print("=" * 70)
cursor.execute("SELECT total_invested, avg_entry_price, open_qty, cycle_id FROM trades WHERE bot_id = 100001")
r = cursor.fetchone()
if r:
    print(f"  total_invested={r[0]}, avg_entry_price={r[1]}, open_qty={r[2]}, cycle_id={r[3]}")

print("\n" + "=" * 70)
print("PART 4: Legacy reset_cleared rows for bot 100001 with filled > 0")
print("=" * 70)
cursor.execute("""
SELECT cycle_id, order_type, SUM(filled_amount) as total, COUNT(*) as cnt
FROM bot_orders
WHERE bot_id = 100001
  AND status = 'reset_cleared'
  AND filled_amount > 0
  AND wipe_proof_source = 'legacy_wipe'
GROUP BY cycle_id, order_type
ORDER BY cycle_id DESC, total DESC
""")
for r in cursor.fetchall():
    print(f"  cycle={r[0]} | type={r[1]:20} | total_filled={r[2]:.4f} | rows={r[3]}")

conn.close()
