"""Full state dump for BTC and Gold bots + order counts."""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection
conn = get_connection()

# Focus on BTC and Gold
focus_bots = conn.execute("""
    SELECT b.id, b.pair, b.direction, b.base_size, b.martingale_multiplier,
           t.current_step, t.entry_confirmed, t.total_invested,
           t.avg_entry_price, t.basket_start_time, t.cycle_id
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE (b.pair LIKE '%BTC%' OR b.pair LIKE '%XAU%' OR b.pair LIKE '%SOL%')
      AND b.is_active=1
    ORDER BY b.pair, b.direction
""").fetchall()

for r in focus_bots:
    bot_id, pair, direction, base_size, mm = r[0], r[1], r[2], r[3], r[4]
    step, confirmed, invested, avg, bst, cycle_id = r[5], r[6], r[7], r[8], r[9], r[10]

    print(f"\n{'='*60}")
    print(f"Bot {bot_id} | {pair} {direction} | base={base_size} mm={mm}")
    print(f"  step={step} confirmed={confirmed} invested={invested:.2f} avg={avg:.4f}")
    print(f"  basket_start_time={bst} cycle_id={cycle_id}")

    # Open orders count
    open_cnt = conn.execute("""
        SELECT COUNT(*), order_type FROM bot_orders
        WHERE bot_id=? AND status IN ('open','new','placing')
        GROUP BY order_type
    """, (bot_id,)).fetchall()
    print(f"  Open orders by type: {dict(open_cnt) if open_cnt else 'NONE'}")

    # Orders this cycle summary
    cycle_summary = conn.execute("""
        SELECT order_type, status, COUNT(*) as cnt
        FROM bot_orders WHERE bot_id=? AND (cycle_id=? OR cycle_id IS NULL)
        GROUP BY order_type, status
        ORDER BY order_type, status
    """, (bot_id, cycle_id)).fetchall()
    print(f"  Cycle {cycle_id} order summary:")
    for cs in cycle_summary:
        print(f"    type={cs[0]:<15} status={cs[1]:<12} count={cs[2]}")

    # Entry order details
    entries = conn.execute("""
        SELECT order_type, step, price, filled_amount, amount, status, client_order_id
        FROM bot_orders WHERE bot_id=? AND order_type IN ('entry','grid','adoption')
          AND filled_amount > 0
        ORDER BY step
    """, (bot_id,)).fetchall()
    print(f"  Filled entry/grid orders:")
    if entries:
        for e in entries:
            print(f"    type={e[0]} step={e[1]} px={e[2]} filled={e[3]}/{e[4]} status={e[5]} cid={str(e[6])[:40]}")
    else:
        print("    NONE — bot has no confirmed fills in bot_orders!")

conn.close()
print('\nDone.')
