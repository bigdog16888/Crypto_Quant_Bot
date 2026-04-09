"""
Deep diagnostic: why does recompute_invested_from_orders return 0 for BTC/SOL/Gold?
Shows exactly what bot_orders exist and what the recompute query finds.
"""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

for bot_id, label in [(10016, 'BTC LONG'), (10008, 'SOL LONG'), (10019, 'XAU SHORT')]:
    print(f"\n{'='*60}")
    print(f"Bot {bot_id} ({label})")

    # What cycle_id is in trades?
    t = conn.execute("SELECT cycle_id, current_step, total_invested, avg_entry_price, entry_confirmed FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    print(f"  trades: cycle_id={t[0]} step={t[1]} invested={t[2]} avg={t[3]} confirmed={t[4]}")

    # What bot_orders exist?
    all_orders = conn.execute("""
        SELECT order_type, step, price, filled_amount, amount, status, cycle_id, client_order_id
        FROM bot_orders WHERE bot_id=?
        ORDER BY step, created_at DESC
    """, (bot_id,)).fetchall()
    print(f"  Total bot_orders: {len(all_orders)}")

    # Summary by status and cycle_id
    from collections import Counter
    status_cnt = Counter((o[5], o[6]) for o in all_orders)
    for (status, cid), cnt in sorted(status_cnt.items()):
        print(f"    status={status} cycle_id={cid}: {cnt} orders")

    # Specifically: filled entry/grid orders
    filled = [(o[0], o[1], o[2], o[3], o[6], o[7]) for o in all_orders if o[3] > 0 and o[0] in ('entry','grid','adoption')]
    print(f"  Filled entry/grid orders ({len(filled)}):")
    for f in filled[:10]:
        print(f"    type={f[0]} step={f[1]} px={f[2]} filled={f[3]} cycle_id={f[4]} cid={str(f[5])[:35]}")

    # Exactly what recompute_invested_from_orders queries
    cycle_id = t[0] or 1
    res = conn.execute("""
        SELECT
            COALESCE(SUM(bo.price * bo.filled_amount), 0),
            COALESCE(SUM(bo.filled_amount), 0),
            COALESCE(MAX(bo.step), 0)
        FROM bot_orders bo
        WHERE bo.bot_id = ?
          AND bo.cycle_id = ?
          AND bo.filled_amount > 0
          AND bo.order_type IN ('entry', 'grid', 'adoption')
          AND bo.status IN ('filled', 'closed', 'partially_filled')
    """, (bot_id, cycle_id)).fetchone()
    print(f"  recompute query result (cycle_id={cycle_id}): cost={res[0]:.2f} qty={res[1]:.6f} max_step={res[2]}")

    # Also check with cycle_id=NULL
    res_null = conn.execute("""
        SELECT
            COALESCE(SUM(bo.price * bo.filled_amount), 0),
            COALESCE(SUM(bo.filled_amount), 0),
            COALESCE(MAX(bo.step), 0)
        FROM bot_orders bo
        WHERE bo.bot_id = ?
          AND bo.cycle_id IS NULL
          AND bo.filled_amount > 0
          AND bo.order_type IN ('entry', 'grid', 'adoption')
          AND bo.status IN ('filled', 'closed', 'partially_filled')
    """, (bot_id,)).fetchone()
    print(f"  recompute query (cycle_id=NULL): cost={res_null[0]:.2f} qty={res_null[1]:.6f} max_step={res_null[2]}")

conn.close()
print("\nDone.")
