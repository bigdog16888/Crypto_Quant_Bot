"""
Apply recompute_invested_from_orders correction to all IN TRADE bots
to align the trades table with the ground-truth bot_orders ledger.
"""
import sys
sys.path.insert(0, '.')
from engine.database import recompute_invested_from_orders, get_connection

conn = get_connection()
bots = conn.execute("""
    SELECT b.id, b.name, t.total_invested, t.avg_entry_price, t.current_step
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE t.total_invested > 0 AND b.is_active=1
""").fetchall()
conn.close()

print(f"{'Bot':>6} {'Name':15} {'Old Invested':>13} {'New Invested':>13} {'Diff':>10} {'Status'}")
print("-"*70)

for bot_id, name, old_inv, old_avg, old_step in bots:
    new_inv, new_avg, new_step = recompute_invested_from_orders(bot_id)
    diff = new_inv - float(old_inv or 0)
    
    if new_inv > 0 and abs(diff) > 0.50:  # Only fix if >$0.50 discrepancy
        conn = get_connection()
        conn.execute("""
            UPDATE trades SET total_invested=?, avg_entry_price=?, current_step=?
            WHERE bot_id=?
        """, (new_inv, new_avg, new_step, bot_id))
        conn.commit()
        conn.close()
        status = f"✅ FIXED"
    elif new_inv > 0:
        status = "✅ OK"
    else:
        status = "⏭️ SKIPPED (recompute=0)"
    
    print(f"{bot_id:>6} {str(name):15} ${float(old_inv or 0):>12.2f} ${new_inv:>12.2f} ${diff:>+9.2f} {status}")
