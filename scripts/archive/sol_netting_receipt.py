import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

# Bot 100001 cycle 26 entry timestamps (from earlier query):
# 189742443: entry 0.11 @ ts=1778735516
# 189742929: grid  0.20 @ ts=1778735577
# 189752443: grid  0.38 @ ts=1778736843
# Window: bracket the full short-building period with margin

print("=" * 90)
print("QUERY 1: Bot 10008 LONG entry fills in window bracketing 100001's 0.69 SHORT build")
print("  Window: 1778700000 (before first fill) to 1778750000 (after last fill)")
print("=" * 90)
cursor.execute("""
SELECT 
    bo.bot_id,
    bo.order_id,
    bo.order_type,
    bo.filled_amount,
    bo.price,
    bo.status,
    bo.cycle_id,
    bo.created_at,
    bo.wipe_proof_source
FROM bot_orders bo
WHERE bo.bot_id = 10008
  AND bo.filled_amount > 0
  AND bo.order_type IN ('entry', 'grid', 'carry', 'adoption', 'adoption_add')
  AND bo.created_at BETWEEN 1778700000 AND 1778750000
ORDER BY bo.created_at ASC;
""")
rows = cursor.fetchall()
total_q1 = 0
for r in rows:
    print(f"  bot={r[0]} | id={r[1]} | type={r[2]:15} | filled={r[3]:.4f} | price={r[4]:.4f} | status={r[5]:15} | cycle={r[6]} | ts={r[7]} | proof={r[8]}")
    total_q1 += r[3]
if not rows:
    print("  NONE in this window")
print(f"  => Total LONG fills in window: {total_q1:.4f} SOL\n")

print("=" * 90)
print("QUERY 2: Bot 10008 reset_cleared fills with filled > 0 for cycles 5–22")
print("=" * 90)
cursor.execute("""
SELECT 
    bo.bot_id,
    bo.order_id,
    bo.order_type,
    bo.filled_amount,
    bo.price,
    bo.status,
    bo.cycle_id,
    bo.created_at,
    bo.wipe_proof_source
FROM bot_orders bo
WHERE bo.bot_id = 10008
  AND bo.status = 'reset_cleared'
  AND bo.filled_amount > 0
  AND bo.cycle_id BETWEEN 5 AND 22
ORDER BY bo.created_at ASC;
""")
rows2 = cursor.fetchall()
total_q2_entry = 0
total_q2_tp    = 0
for r in rows2:
    otype = r[2]
    filled = r[3]
    side = "ENTRY" if otype in ('entry','grid','carry','adoption','adoption_add') else "EXIT"
    if side == "ENTRY":
        total_q2_entry += filled
    else:
        total_q2_tp += filled
    print(f"  [{side}] bot={r[0]} | id={r[1]} | type={r[2]:15} | filled={filled:.4f} | price={r[4]:.4f} | status={r[5]:15} | cycle={r[6]} | ts={r[7]} | proof={r[8]}")
print(f"\n  => Total ENTRY-side wiped (cycles 5-22): {total_q2_entry:.4f} SOL")
print(f"  => Total EXIT-side wiped  (cycles 5-22): {total_q2_tp:.4f} SOL")
net_wiped = total_q2_entry - total_q2_tp
print(f"  => Net LONG exposure wiped (entry - exit): {net_wiped:.4f} SOL")

print("\n" + "=" * 90)
print("QUESTION ANSWERS")
print("=" * 90)
print(f"  (1) Do bot 10008 LONG fills cycles 5-22 sum to ~0.22?  => {total_q2_entry:.4f} entry / {net_wiped:.4f} net")
print(f"  (2) All tagged legacy_wipe?", end=" ")
cursor.execute("""
    SELECT COUNT(*) FROM bot_orders 
    WHERE bot_id=10008 AND status='reset_cleared' AND filled_amount>0 
    AND cycle_id BETWEEN 5 AND 22 AND wipe_proof_source != 'legacy_wipe'
""")
non_legacy = cursor.fetchone()[0]
print(f"{'YES' if non_legacy == 0 else f'NO — {non_legacy} rows have different proof'}")

print(f"  (3) Any TP/close receipts on bot 10008 side?")
cursor.execute("""
    SELECT order_type, SUM(filled_amount), COUNT(*) 
    FROM bot_orders
    WHERE bot_id=10008 AND filled_amount>0
      AND order_type IN ('tp','close','sl','dust_close','adoption_reduce')
      AND cycle_id BETWEEN 5 AND 22
    GROUP BY order_type
""")
tp_rows = cursor.fetchall()
if tp_rows:
    for r in tp_rows:
        print(f"      type={r[0]:20} | total={r[1]:.4f} | rows={r[2]}")
else:
    print("      NONE — no TP/close receipts found for bot 10008 cycles 5-22")

conn.close()
