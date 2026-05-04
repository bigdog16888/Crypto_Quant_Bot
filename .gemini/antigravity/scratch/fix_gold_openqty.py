"""
AUTO-APPLY Surgical fix for bot 10019 (short gold) TP Cancel Storm.
Run while engine is running - it's safe (just corrects open_qty and marks ghost as archived).
"""
import sqlite3, time, sys
sys.path.insert(0, '.')

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("SURGICAL FIX: Bot 10019 open_qty Correction")

# Verify state before fix
cur.execute("SELECT open_qty, total_invested, avg_entry_price, cycle_id FROM trades WHERE bot_id=10019")
t = dict(cur.fetchone())
print(f"BEFORE: open_qty={t['open_qty']}, invested={t['total_invested']}, avg={t['avg_entry_price']}")

# The ONLY real entry for this cycle is 92536: order 48550289 @ 4728.99, 0.005 XAU
# Order 92557 (48549641) is a history-orphan from cycle 1 that was already TP'd
real_entry_price = 4728.99
real_entry_qty   = 0.005
real_invested    = round(real_entry_price * real_entry_qty, 8)

now = int(time.time())

# Step 1: Archive the ghost history-orphan entry
cur.execute("UPDATE bot_orders SET status='reset_cleared', updated_at=? WHERE id=92557", (now,))
print(f"  ✅ Order 92557 (history-orphan 48549641) -> reset_cleared")

# Step 2: Correct the trades ledger
cur.execute("""UPDATE trades SET open_qty=?, total_invested=?, avg_entry_price=?
    WHERE bot_id=10019""", (real_entry_qty, real_invested, real_entry_price))
print(f"  ✅ trades: open_qty={real_entry_qty}, invested={real_invested}, avg={real_entry_price}")

conn.commit()

cur.execute("SELECT open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id=10019")
t = dict(cur.fetchone())
print(f"AFTER: open_qty={t['open_qty']}, invested={t['total_invested']}, avg={t['avg_entry_price']}")
print("Done. SYNC-DRIFT storm will stop next cycle.")
conn.close()
