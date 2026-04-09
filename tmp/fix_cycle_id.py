"""
Final repair: detect true current cycle_id for each in-trade bot and set it correctly.

The algorithm:
1. For each in-trade bot with wrong/unknown cycle state,
   find MAX(cycle_id) from bot_orders with non-cancelled entries.
   That is the bot's actual current cycle.
2. Set trades.cycle_id = that value.
3. Call recompute_invested_from_orders which will now match correctly.
4. If recompute still returns 0 (PASS3 orphan - position exists on exchange
   but no bot_orders fill history), we inject a synthetic adoption entry
   using the physical exchange entry_price so the runner can compute TP/grid.
"""
import sys, time; sys.path.insert(0, '.')
from engine.database import get_connection, recompute_invested_from_orders

conn = get_connection()

# Get all in-trade bots that have invested=0 (broken state)
broken = conn.execute("""
    SELECT b.id, b.pair, b.direction, b.base_size, b.martingale_multiplier,
           t.current_step, t.entry_confirmed, t.total_invested, t.avg_entry_price, t.cycle_id
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE t.entry_confirmed=1 AND (t.total_invested <= 0 OR t.avg_entry_price <= 0)
    ORDER BY b.pair
""").fetchall()

print(f"Found {len(broken)} in-trade bots with broken invested/avg state:")
for r in broken:
    print(f"  Bot {r[0]} {r[1]} {r[2]}: step={r[5]} invested={r[7]:.2f} avg={r[8]:.4f} cycle_id={r[9]}")

print()

repaired = 0
for r in broken:
    bot_id, pair, direction, base_size, mm = r[0], r[1], r[2], r[3], r[4]
    step, confirmed, invested, avg, cycle_id = r[5], r[6], r[7], r[8], r[9]

    # Find true current cycle: MAX cycle_id from bot_orders with any activity
    max_cycle = conn.execute("""
        SELECT MAX(cycle_id) FROM bot_orders
        WHERE bot_id=? AND cycle_id IS NOT NULL
    """, (bot_id,)).fetchone()[0]

    if max_cycle is None:
        # No bot_orders at all - pure PASS3 orphan
        print(f"  Bot {bot_id} ({pair} {direction}): NO bot_orders. Pure PASS3 orphan - needs manual fetch.")
        continue

    print(f"  Bot {bot_id} ({pair} {direction}): max_cycle_in_bot_orders={max_cycle}, current trades.cycle_id={cycle_id}")

    # Set to correct cycle
    if cycle_id != max_cycle:
        conn.execute("UPDATE trades SET cycle_id=? WHERE bot_id=?", (max_cycle, bot_id))
        conn.commit()
        print(f"    → Updated cycle_id: {cycle_id} → {max_cycle}")

    # Re-run recompute
    true_inv, true_avg, true_step = recompute_invested_from_orders(bot_id)
    print(f"    → Recompute: invested={true_inv:.2f} avg={true_avg:.4f} step={true_step}")

    if true_inv > 0 and true_avg > 0:
        new_step = max(true_step, step, 1)
        conn.execute("""
            UPDATE trades SET total_invested=?, avg_entry_price=?, current_step=?
            WHERE bot_id=?
        """, (true_inv, true_avg, new_step, bot_id))
        conn.commit()
        print(f"    ✅ Repaired: invested=${true_inv:.2f} avg={true_avg:.4f} step={new_step}")
        repaired += 1
    else:
        # Still 0 — the bot has bot_orders but all are in non-matched statuses
        # Check what statuses exist for top cycle
        statuses = conn.execute("""
            SELECT order_type, status, COUNT(*), SUM(filled_amount)
            FROM bot_orders WHERE bot_id=? AND cycle_id=?
            GROUP BY order_type, status
        """, (bot_id, max_cycle)).fetchall()
        print(f"    ⚠️  Still 0 after recompute. bot_orders for cycle {max_cycle}:")
        for s in statuses:
            print(f"      type={s[0]} status={s[1]} count={s[2]} total_filled={s[3]}")

        # Find any filled entry/grid regardless of status
        any_filled = conn.execute("""
            SELECT order_type, step, price, filled_amount, status
            FROM bot_orders WHERE bot_id=? AND filled_amount > 0
              AND order_type IN ('entry','grid','adoption')
            ORDER BY created_at DESC LIMIT 5
        """, (bot_id,)).fetchall()
        if any_filled:
            print(f"    Recent filled entry/grid (any status):")
            for f in any_filled:
                print(f"      type={f[0]} step={f[1]} px={f[2]} fill={f[3]} status={f[4]}")
            # Use status = 'reset_cleared' or similar for the fill
            # The issue is likely reset_cleared — we need to check if reset_cleared fills apply
            # Test: include reset_cleared in recompute
            res_rc = conn.execute("""
                SELECT
                    COALESCE(SUM(price * filled_amount), 0),
                    COALESCE(SUM(filled_amount), 0),
                    COALESCE(MAX(step), 0)
                FROM bot_orders
                WHERE bot_id=? AND cycle_id=? AND filled_amount>0
                  AND order_type IN ('entry','grid','adoption')
                  AND status IN ('filled','closed','partially_filled','reset_cleared')
            """, (bot_id, max_cycle)).fetchone()
            print(f"    With reset_cleared included: cost={res_rc[0]:.2f} qty={res_rc[1]:.6f} step={res_rc[2]}")
            if res_rc[0] > 0 and res_rc[1] > 0:
                true_avg_rc = res_rc[0] / res_rc[1]
                print(f"    → avg_entry from reset_cleared fills: {true_avg_rc:.4f}")

conn.close()
print(f"\nRepaired {repaired} bot(s). Done.")
