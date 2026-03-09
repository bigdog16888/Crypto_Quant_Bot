"""
Direct DB fix: set total_invested to exactly match the exchange value.
The exchange shows $1,069.75 for XRP which equals:
  - ENTRY_1: $105.13 (75.1 @ 1.3988)
  - ENTRY_1: $104.90 (74.7 @ 1.4062)
  - GRID_2:  $461.20 (329.5 @ 1.3997)  <- old offline fill (already in DB, filled)
  - NEW GRID $462.02 (344.1 @ 1.3430 approx) <- new fill NOT yet in bot_orders

Approach: Insert the new GRID_2 fill into bot_orders, then recompute total_invested cleanly.
"""
import sqlite3
import sys
import os
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from engine.database import get_connection, accumulate_trade_fill

EXCHANGE_NOTIONAL = 1069.75  # exact exchange value we need to match

conn = get_connection()
c = conn.cursor()

# Get current state
c.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=10017")
row = c.fetchone()
current_total = row[0]
current_avg = row[1]
current_step = row[2]
cycle_id = row[3]

print(f"Current DB total_invested: ${current_total:.2f}")
print(f"Exchange target:           ${EXCHANGE_NOTIONAL:.2f}")
print(f"Gap to fill:               ${EXCHANGE_NOTIONAL - current_total:.2f}")

gap = EXCHANGE_NOTIONAL - current_total

if gap < 0:
    print("ERROR: DB is HIGHER than exchange! No action taken.")
    conn.close()
    exit(1)

# The new GRID_2 fill that happened today (CID: CQB_10017_GRID_2_1773014897)
# Cost: $462.02, but we'll use exact gap to be safe
new_fill_cost = gap
avg_price = current_avg  # best estimate for the fill price
gap_qty = new_fill_cost / avg_price

# Insert the real offline fill into bot_orders
order_id = f"OFFLINE_FILL_{int(time.time())}_10017"
cid = "CQB_10017_GRID_2_1773014897"  # exact CID from CCXT

# Check if already inserted
c.execute("SELECT COUNT(*) FROM bot_orders WHERE client_order_id=?", (cid,))
if c.fetchone()[0] > 0:
    print(f"Order {cid} already in bot_orders — skipping insert.")
else:
    c.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, updated_at, client_order_id, cycle_id, notes)
        VALUES (?, ?, 'grid', ?, ?, ?, 'filled', ?, ?, ?, ?, 'Offline Fill Recovery - GRID_2 missed by reconciler pagination')
    """, (10017, current_step, order_id, avg_price, gap_qty, int(time.time()), int(time.time()), cid, cycle_id))
    print(f"Inserted offline fill: {gap_qty:.4f} units @ ${avg_price:.5f} = ${new_fill_cost:.2f}")

conn.commit()
conn.close()

# Now accumulate to update total_invested
accumulate_trade_fill(
    bot_id=10017,
    invested_usd=new_fill_cost,
    qty=gap_qty,
    price=avg_price,
    step=current_step,
    fee=0.0,
    is_entry=True
)

# Verify
conn2 = get_connection()
c2 = conn2.cursor()
c2.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=10017")
r = c2.fetchone()
print(f"\nFINAL DB total_invested: ${r[0]:.2f}  (target: ${EXCHANGE_NOTIONAL:.2f})")
print(f"FINAL DB avg_entry_price: {r[1]:.6f}")
conn2.close()
