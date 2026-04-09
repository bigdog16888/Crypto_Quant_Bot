"""
Quick verification: show what recompute_invested_from_orders returns for
affected bots, then run _align_memory_to_ledger() and show the corrected trades.
"""
import sys
sys.path.insert(0, '.')

from engine.database import recompute_invested_from_orders, sync_trades_from_orders, get_connection

bots = [(10008, 'sol LONG'), (10017, 'xrp long'), (10018, 'sui long'), (10016, 'btc LONG'), (100000, 'short sui')]

print("=== RECOMPUTE vs DB (BEFORE ALIGNMENT) ===\n")
conn = get_connection()
for bot_id, name in bots:
    # What does recompute say?
    cost, avg, step = recompute_invested_from_orders(bot_id)
    # What does the DB say?
    row = conn.execute("SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    db_inv = float(row[0] or 0) if row else 0
    db_avg = float(row[1] or 0) if row else 0
    db_step = int(row[2] or 0) if row else 0
    db_qty = db_inv / db_avg if db_avg > 0 else 0
    recomp_qty = cost / avg if avg > 0 else 0
    delta = abs(recomp_qty - db_qty)

    print(f"Bot {bot_id} ({name}):")
    print(f"  DB:      invested=${db_inv:.2f}  avg=${db_avg:.4f}  step={db_step}  qty={db_qty:.4f}")
    print(f"  Recomp:  invested=${cost:.2f}  avg=${avg:.4f}  step={step}  qty={recomp_qty:.4f}")
    print(f"  Delta qty: {delta:.6f}  {'⚠️ DRIFT' if delta > 1e-6 else '✅ IN SYNC'}")
    print()
conn.close()

print("=== RUNNING sync_trades_from_orders (DIRECT FIX) ===\n")
for bot_id, name in bots:
    fixed = sync_trades_from_orders(bot_id)
    print(f"Bot {bot_id} ({name}): {'CORRECTED ✅' if fixed else 'Already in sync'}")

print("\n=== DB STATE AFTER FIX ===\n")
conn = get_connection()
for bot_id, name in bots:
    row = conn.execute("SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    inv = float(row[0] or 0) if row else 0
    avg = float(row[1] or 0) if row else 0
    step = int(row[2] or 0) if row else 0
    qty = inv / avg if avg > 0 else 0
    print(f"Bot {bot_id} ({name}): invested=${inv:.2f}  qty={qty:.4f}  step={step}")
conn.close()
