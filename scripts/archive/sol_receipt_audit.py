import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

cursor.execute("""
SELECT order_id, order_type, filled_amount, price, status, 
       cycle_id, created_at, wipe_proof_source
FROM bot_orders
WHERE bot_id IN (100001, 10008)
  AND filled_amount > 0
  AND status IN ('filled', 'reset_cleared', 'auto_closed')
  AND cycle_id = 26
ORDER BY created_at ASC;
""")
rows = cursor.fetchall()

print(f"{'order_id':>15} | {'bot/type':>25} | {'filled':>8} | {'price':>8} | {'status':>15} | {'cycle':>5} | {'created_at':>12} | proof")
print("-" * 120)

# Also need to know which bot each row belongs to — let's get that info
cursor2 = conn.cursor()
for r in rows:
    order_id, order_type, filled, price, status, cycle_id, created_at, proof = r
    # look up bot_id for this order
    cursor2.execute("SELECT bot_id FROM bot_orders WHERE order_id = ? AND cycle_id = 26", (order_id,))
    brow = cursor2.fetchone()
    bot_id = brow[0] if brow else "?"
    print(f"{str(order_id):>15} | bot={bot_id:<6} {order_type:>16} | {filled:>8.4f} | {price:>8.4f} | {status:>15} | {cycle_id:>5} | {created_at:>12} | {proof}")

print(f"\nTotal rows: {len(rows)}")
print("\n--- Accounting summary ---")

# Sum by bot and type
cursor.execute("""
SELECT bo.bot_id, bo.order_type, bo.status, 
       SUM(bo.filled_amount) as total,
       COUNT(*) as cnt
FROM bot_orders bo
WHERE bo.bot_id IN (100001, 10008)
  AND bo.filled_amount > 0
  AND bo.status IN ('filled', 'reset_cleared', 'auto_closed')
  AND bo.cycle_id = 26
GROUP BY bo.bot_id, bo.order_type, bo.status
ORDER BY bo.bot_id, bo.order_type
""")
sums = cursor.fetchall()
for s in sums:
    print(f"  bot={s[0]} | type={s[1]:25} | status={s[2]:15} | total={s[3]:.4f} | rows={s[4]}")

# Net position calc
print("\n--- Net position math ---")
cursor.execute("""
SELECT bo.bot_id, 
       (SELECT direction FROM bots WHERE id = bo.bot_id) as dir,
       bo.order_type, 
       SUM(bo.filled_amount) as total
FROM bot_orders bo
WHERE bo.bot_id IN (100001, 10008)
  AND bo.filled_amount > 0
  AND bo.status IN ('filled', 'reset_cleared', 'auto_closed')
  AND bo.cycle_id = 26
GROUP BY bo.bot_id, bo.order_type
""")
entries = {}
exits = {}
for row in cursor.fetchall():
    bot_id, direction, otype, total = row
    key = f"bot{bot_id}({direction})"
    if otype in ('entry', 'grid', 'adoption_add', 'adoption', 'carry'):
        entries[key] = entries.get(key, 0) + total
        print(f"  [ENTRY-SIDE] {key} | +{total:.4f} ({otype})")
    elif otype in ('tp', 'close', 'dust_close', 'sl', 'adoption_reduce', 'virtual_netting'):
        exits[key] = exits.get(key, 0) + total
        print(f"  [EXIT-SIDE]  {key} | -{total:.4f} ({otype})")

print(f"\n  Exchange holds: 0.3900 SHORT (net)")
print(f"  Bot 10008 is LONG  → offsets short by +0.08")
print(f"  Bot 100001 is SHORT → contributes to short")

conn.close()
