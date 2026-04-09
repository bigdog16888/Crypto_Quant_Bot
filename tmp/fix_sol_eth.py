import sqlite3
import time

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

now = int(time.time())

print("=== FIX 1: Restore sol adoption order for cycle 12 ===")
# The adoption for sol cycle 12 exists but is reset_cleared — restore it
q.execute("SELECT rowid, filled_amount FROM bot_orders WHERE bot_id=10008 AND order_type='adoption' AND cycle_id=12 AND status='reset_cleared' ORDER BY created_at DESC LIMIT 1")
row = q.fetchone()
if row:
    rowid, qty = row
    q.execute("UPDATE bot_orders SET status='filled', updated_at=? WHERE rowid=?", (now, rowid))
    print(f"  ✅ Restored adoption order (rowid={rowid}, qty={qty}) to 'filled'")
else:
    # No adoption exists for cycle 12 at all — inject one
    print("  Creating new adoption for sol cycle 12...")
    # sol has 2.74 SOL physical, p2 proven qty should cover most of it
    # The existing adoption_add orders from previous sessions cover the old proven qty
    # We'll create a new adoption for the gap — use total_invested/avg_price
    q.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=10008")
    inv, avg = q.fetchone()
    expected_qty = inv / avg if avg > 0 else 2.74
    synthetic_oid = f"PASS3_ADOPTION_10008_C12_MANUAL"
    synthetic_cid = f"CQB_10008_PASS3_C12_MANUAL"
    # Use current sol price ~$90
    sol_price = avg if avg > 0 else 90.0
    q.execute("""
        INSERT OR IGNORE INTO bot_orders 
        (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, step, cycle_id, created_at, updated_at)
        VALUES (10008, ?, ?, 'adoption', ?, ?, ?, 'filled', 10, 12, ?, ?)
    """, (synthetic_oid, synthetic_cid, sol_price, expected_qty, expected_qty, now, now))
    print(f"  ✅ Injected adoption order for sol cycle 12: qty={expected_qty:.4f} @ {sol_price:.2f}")

print()
print("=== FIX 2: Cancel stale cycle-46 TP for sol (in DB) ===")
q.execute("SELECT order_id FROM bot_orders WHERE bot_id=10008 AND order_type='tp' AND cycle_id=46 AND status='new'")
row = q.fetchone()
if row:
    oid = row[0]
    # Mark as cancelled in DB — the exchange order may already be stale
    q.execute("UPDATE bot_orders SET status='cancelled', updated_at=? WHERE bot_id=10008 AND order_type='tp' AND cycle_id=46 AND status='new'", (now,))
    print(f"  ✅ Cancelled stale cycle-46 TP in DB (order_id={oid})")
    print(f"  ⚠️  NOTE: Also cancel this on exchange if still open: order_id={oid}")
else:
    print("  (No stale cycle-46 TP found)")

print()
print("=== FIX 3: Correct short_eth total_invested ===")
# Physical: 0.043 ETH @ ~$2033.75 = $87.45
# But trades shows $109.82 which is wrong
# Correct it to match physical qty × avg_entry
q.execute("SELECT total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=100002")
row = q.fetchone()
print(f"  Current: invested={row[0]:.2f} avg={row[1]:.4f} step={row[2]} cycle={row[3]}")
# Physical is 0.043 @ 2033.75 = $87.45
# But bot_orders has entry filled 0.017 ETH — that's just the recent entry
# The total is 0.043 on exchange. The PASS-3 adoption likely computed phys_invested wrongly
# Correct total_invested = 0.043 * avg_entry_price = 0.043 * 2033.76 = ~87.45
corrected_inv = round(0.043 * row[1], 4)
print(f"  Corrected: invested={corrected_inv:.2f} (0.043 × {row[1]:.4f})")
q.execute("UPDATE trades SET total_invested=? WHERE bot_id=100002", (corrected_inv,))
print(f"  ✅ Updated short_eth total_invested to {corrected_inv:.4f}")

c.commit()
c.close()

print()
print("=== Verification ===")
c2 = sqlite3.connect('crypto_bot.db')
q2 = c2.cursor()
q2.execute("SELECT order_type,status,filled_amount,cycle_id FROM bot_orders WHERE bot_id=10008 AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled') ORDER BY created_at DESC LIMIT 5")
print("sol active orders:", q2.fetchall())
q2.execute("SELECT total_invested,avg_entry_price FROM trades WHERE bot_id=100002")
print("short_eth trades:", q2.fetchone())
c2.close()
