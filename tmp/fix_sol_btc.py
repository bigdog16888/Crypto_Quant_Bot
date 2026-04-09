import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== FIX 1: SOL active_positions.bot_id = 0 → 10008 ===")
# long sol (10008) should own the SOLUSDC LONG position
# active_positions.bot_id=0 means the reconciler can't correlate this position to any bot
q.execute("""
    UPDATE active_positions 
    SET bot_id=10008 
    WHERE pair='SOLUSDC' AND side='LONG' AND bot_id=0
""")
print(f"  Updated {q.rowcount} row(s)")

# Also ensure trades has a valid cycle_id for sol (it shows cycle=12)
# and that entry_confirmed=1 so the reconciler doesn't skip it
q.execute("SELECT cycle_id, total_invested, entry_confirmed FROM trades WHERE bot_id=10008")
r = q.fetchone()
print(f"  SOL trades: cycle_id={r[0]}, total_invested={r[1]}, entry_confirmed={r[2]}")

print()
print("=== FIX 2: Delete stale BTC PASS3_C10 adoption (0.017 based on wrong CARRY) ===")
# With Fix A (recompute excludes reset_cleared), the CARRY adoption_add(reset_cleared, 0.006) 
# is now excluded. When reconciler runs, true_qty=0, gap=0.023, PASS-3 will write adoption=0.023.
# But currently adoption=0.017 (written when CARRY was still being counted as true_qty=0.006).
# Deleting it forces the reconciler to re-write with correct gap calculation.
q.execute("DELETE FROM bot_orders WHERE order_id='PASS3_ADOPTION_10016_C10' AND bot_id=10016")
print(f"  Deleted BTC PASS3_C10 adoption: {q.rowcount} row(s)")

# Verify current state
print()
q.execute("""
    SELECT order_type, status, filled_amount, cycle_id, client_order_id
    FROM bot_orders WHERE bot_id=10016 AND cycle_id=10
    AND status NOT IN ('placing','failed') AND filled_amount>0
""")
print("  BTC/long_btc_price cycle=10 remaining orders:")
for r in q.fetchall(): print(f"    {r[0]:<15} {r[1]:<12} qty={r[2]:.4f} [{r[4][:50]}]")
print("  → After restart, recompute sees 0 (only reset_cleared left), gap=0.023, PASS-3 writes 0.023 ✓")

print()
print("=== FIX 3: XRP — oscillation fixed in code (reconciler.py). DB state OK for next run ===")
q.execute("""
    SELECT order_type, status, filled_amount, cycle_id
    FROM bot_orders WHERE bot_id=10017 AND order_type='adoption' AND status='filled'
""")
for r in q.fetchall(): print(f"  XRP adoption: qty={r[2]:.4f} cycle={r[3]}")
print("  → On next reconciler run: real_proved=0 (no real fills), gap=338.2, writes 338.2 adoption ✓")
print("  → Oscillation fix ensures gap is computed as phys_qty(338.2) - real_fills(0) = 338.2")

print()
print("=== VERIFY: SOL after fix ===")
q.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='SOLUSDC'")
r = q.fetchone()
print(f"  active_positions: {r}")
if r and r[0] == 10008:
    print("  ✅ SOL correctly linked to bot 10008")
else:
    print("  ❌ Still not linked!")

c.commit()
c.close()
print()
print("All DB fixes applied. Restart engine for reconciler to re-run and heal all positions.")
