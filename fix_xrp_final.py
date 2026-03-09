"""
Insert all missing filled orders into bot_orders for XRP bot 10017,
then recompute total_invested and avg_entry_price from scratch.

Known fills from CCXT (excluding the TP which closed cycle 1):
Current cycle fills (after TP filled and new cycle started):
  CQB_10017_ENTRY_1_1772781477  qty=75.0 @ 1.3987  cost=$104.90
  CQB_10017_GRID_2_1772784150   qty=329.5 @ 1.3997  cost=$461.20  <-- MISSING from DB
  CQB_10017_GRID_2_1773014897   qty=344.1 @ 1.3427  cost=$462.02  <-- already in DB
  (also CQB_10017_ENTRY_1_1772782278 qty=74.7 @ 1.4062 cost=$105.04 -- check if in DB)
"""
import sys, os, time
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("SELECT cycle_id, current_step FROM trades WHERE bot_id=10017")
trade = c.fetchone()
cycle_id, current_step = trade[0], trade[1]
print(f"Bot 10017 cycle_id={cycle_id} current_step={current_step}")

# All fills we know should be in the current cycle (post-TP reset at CQB_10017_TP_5_1772781426)
# TP_5 was the close of old cycle, new cycle starts with ENTRY_1_1772781477
known_fills = [
    ("CQB_10017_ENTRY_1_1772781477", "entry", 1, 75.0,  1.3987),
    ("CQB_10017_ENTRY_1_1772782278", "entry", 1, 74.7,  1.4062),  # second entry attempt
    ("CQB_10017_GRID_2_1772784150",  "grid",  2, 329.5, 1.3997),  # offline fill (missing!)
    ("CQB_10017_GRID_2_1773014897",  "grid",  2, 344.1, 1.3427),  # this morning's fill
]

inserted = 0
for cid, otype, step, qty, price in known_fills:
    c.execute("SELECT status FROM bot_orders WHERE client_order_id=?", (cid,))
    existing = c.fetchone()
    cost = qty * price
    if existing:
        print(f"  EXISTS [{existing[0]:10}]: {cid} (${cost:.2f})")
    else:
        oid = f"OFFLINE_{cid.split('_')[-1]}_{int(time.time())}_10017"
        c.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, 
                                    created_at, updated_at, client_order_id, cycle_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, 'Offline recovery - CCXT confirmed fill')
        """, (10017, step, otype, oid, price, qty, int(time.time()), int(time.time()), cid, cycle_id))
        inserted += 1
        print(f"  INSERTED: {cid} qty={qty} @ {price} (${cost:.2f})")

conn.commit()
print(f"\nInserted {inserted} missing entries.")

# Now recompute total_invested from scratch
c.execute("""
    SELECT SUM(amount * price), SUM(amount), COUNT(*) FROM bot_orders
    WHERE bot_id=10017 AND cycle_id=? AND status='filled'
      AND order_type IN ('entry','grid')
""", (cycle_id,))
r = c.fetchone()
total_invested = r[0] or 0.0
total_qty = r[1] or 0.0
count = r[2]
avg_price = total_invested / total_qty if total_qty else 0

print(f"\nRecomputed from {count} filled entry/grid orders:")
print(f"  total_invested:  ${total_invested:.2f}")
print(f"  total_qty:       {total_qty:.4f}")
print(f"  avg_entry_price: {avg_price:.6f}")

c.execute("UPDATE trades SET total_invested=?, avg_entry_price=? WHERE bot_id=10017", (total_invested, avg_price))
conn.commit()
conn.close()
print(f"\nDone. DB now: total_invested=${total_invested:.2f}")
