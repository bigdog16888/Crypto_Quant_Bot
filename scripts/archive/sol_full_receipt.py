import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

# Bot 10008 — ALL filled orders across ALL cycles (it's a newer bot, check all)
print("=" * 80)
print("Bot 10008 — ALL filled/closed orders (all cycles, filled > 0)")
print("=" * 80)
cursor.execute("""
SELECT order_id, order_type, filled_amount, price, status, cycle_id, created_at
FROM bot_orders
WHERE bot_id = 10008
  AND filled_amount > 0
  AND status NOT IN ('cancelled', 'canceled', 'failed', 'rejected', 'open', 'new')
ORDER BY created_at ASC
""")
for r in cursor.fetchall():
    print(f"  id={r[0]} | type={r[1]:20} | filled={r[2]:.4f} | price={r[3]:.4f} | status={r[4]:15} | cycle={r[5]} | ts={r[6]}")

# The exchange holds 0.39 SHORT total net
# Bot 10008 is LONG  → bot ledger entry fills are NET LONG
# Bot 100001 is SHORT → bot ledger entry fills are NET SHORT
# In One-Way mode: net = SHORT(100001) - LONG(10008) = exchange net SHORT
#
# Exchange net = 0.39 SHORT
# Bot 10008 LONG filled (cycle 23) = 0.08 LONG  ← already confirmed
# So: 100001_SHORT - 10008_LONG = 0.39
#   → 100001_SHORT_net = 0.39 + 0.08 = 0.47
#
# But 100001's ledger shows: 0.11 (reset_cleared) + 0.58 (filled grids) = 0.69
# That's 0.69 - 0.47 = 0.22 unaccounted on the SHORT side
# Where are the 0.22 SHORT exits (TPs, closes)?

print("\n" + "=" * 80)
print("Bot 100001 — ALL tp/close/sl/adoption_reduce orders cycle 26 (any status with fill)")
print("=" * 80)
cursor.execute("""
SELECT order_id, order_type, filled_amount, price, status, cycle_id, created_at
FROM bot_orders
WHERE bot_id = 100001
  AND filled_amount > 0
  AND order_type IN ('tp', 'close', 'dust_close', 'sl', 'adoption_reduce', 'virtual_netting', 'hedge', 'hedgetp', 'hedge_tp')
  AND (cycle_id = 26 OR cycle_id IS NULL)
ORDER BY created_at ASC
""")
rows = cursor.fetchall()
for r in rows:
    print(f"  id={r[0]} | type={r[1]:20} | filled={r[2]:.4f} | price={r[3]:.4f} | status={r[4]:15} | cycle={r[5]} | ts={r[6]}")
if not rows:
    print("  NONE — no exit fills found for bot 100001 in cycle 26")

print("\n" + "=" * 80)
print("Full receipt math")
print("=" * 80)
# What exchange ACTUALLY holds (ground truth)
# 0.39 SHORT net in One-Way mode
# Bot 10008 (LONG): filled 0.08 in cycle 23
# So 100001 must have NET SHORT position of: 0.39 + 0.08 = 0.47
# 100001's raw fills: entry 0.11 + grid 0.20 + grid 0.38 = 0.69 SHORT opened
# 0.69 - 0.47 = 0.22 must have been CLOSED by TP/netted via One-Way mode
# Question: is there a receipt for 0.22 closes?
print("  Exchange net SHORT  : 0.3900")
print("  Bot 10008 LONG      : 0.0800 (cycle 23, order 189800793)")
print("  => 100001 must net  : 0.3900 + 0.0800 = 0.4700 SHORT")
print("  100001 raw entries  : 0.1100 (entry) + 0.2000 (grid) + 0.3800 (grid) = 0.6900")
print("  Unexplained closure : 0.6900 - 0.4700 = 0.2200 SOL — need receipt for this")

conn.close()
