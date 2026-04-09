import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

# Check if XRP has a TP filled in cycle=42 (to determine if the 3.7 reset_cleared was TP-swept or retry-aborted)
print("=== XRP cycle=42: ALL orders with filled_amount>0 ===")
q.execute("""
    SELECT order_type, status, filled_amount, price, created_at, client_order_id
    FROM bot_orders WHERE bot_id=10017 AND cycle_id=42
    ORDER BY created_at
""")
for r in q.fetchall():
    if r[2] > 0:
        print(f"  {r[0]:<15} {r[1]:<15} qty={r[2]:.4f} price={r[3]:.4f} [{r[5][:60]}]")

print("\n=== KEY: Was the 3.7 XRP entry reset_cleared because a TP fired? ===")
q.execute("""
    SELECT COUNT(*) FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=42 AND order_type='tp' AND status='filled' AND filled_amount>0
""")
tp_count = q.fetchone()[0]
print(f"  TP filled in cycle=42: {tp_count}")

# Also check if entry is a deduplicated retry
q.execute("""
    SELECT client_order_id, status, filled_amount FROM bot_orders 
    WHERE bot_id=10017 AND cycle_id=42 AND order_type='entry'
    ORDER BY created_at
""")
entries = q.fetchall()
print(f"\n  All entry orders in cycle=42:")
for e in entries: print(f"    {e[1]:<15} qty={e[2]:.4f} cid={e[0][:60]}")

# Check SOL active_positions owner vs which bot should own it
print("\n\n=== SOL: Who should own the SOLUSDC position? ===")
q.execute("SELECT id, name, direction, pair FROM bots WHERE pair='SOLUSDC' AND is_active=1")
sol_bots = q.fetchall()
for b in sol_bots: print(f"  Bot: {b}")

q.execute("SELECT * FROM active_positions WHERE pair='SOLUSDC'")
r = q.fetchone()
print(f"  active_positions: {r}")

# The correct bot to own it: long sol (10008) since physical is LONG 2.62
# Check if 10008's trades shows cycle=12
q.execute("SELECT cycle_id, total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=10008")
print(f"  trades for 10008: {q.fetchone()}")

# Sol cycle=12 specific PASS3 adoption check
print("\n=== Does the reconciler know bot_id for SOL? ===")
# active_positions.bot_id=0 means the reconciler sees NO bot for this pair+side
# The reconciler loop processes bots, not positions, so it CAN still process bot 10008
# and check if it has a physical position. The active_positions.bot_id=0 is a separate display issue.
# Let's check if there's any connection between long sol and the orphaned SOL position

# BTC: Understand the 0.006 gap
print("\n\n=== BTC: Full picture of cycle-10 state ===")
print("Expected: total_invested=$1183.74 from adoption(0.017), physical=0.023 BTC")
print("Root: PASS-3 wrote 0.017 because true_qty=0.006 when it ran.")
print("The 0.006 came from adoption_add(CARRY) at cycle=10 which is reset_cleared.")
print("With Fix A, recompute now returns 0 for cycle-10.")
print("But PASS-3 for BTC already ran and wrote 0.017 based on OLD recompute (before Fix A).")
print("To fix: EITHER (a) manually correct via DB, OR (b) delete PASS3 adoption and let reconciler re-run.")
q.execute("""
    SELECT order_type, status, filled_amount, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=10
    AND status NOT IN ('placing','failed')
    ORDER BY created_at
""")
print("  All cycle=10 orders:")
for r in q.fetchall(): print(f"    {r[0]:<15} {r[1]:<15} qty={r[2]:.4f} [{r[4][:60]}]")

# Confirm: if we delete current PASS3 adoption (0.017) and let reconciler re-run with Fix A in place:
# recompute cycle-10 (excl reset_cleared) = 0 → gap = 0.023 → PASS-3 writes 0.023 → CORRECT
print("\n  → If we delete 'CQB_10016_PASS3_C10' adoption and restart, PASS-3 will write 0.023 ✓")

c.close()
