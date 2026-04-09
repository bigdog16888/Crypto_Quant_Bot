"""
Diagnostic: show full state of all in-trade bots and their open orders.
"""
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

print('=== TRADES TABLE (in-trade bots) ===')
rows = conn.execute("""
    SELECT b.id, b.pair, b.direction, b.base_size,
           t.current_step, t.entry_confirmed, t.total_invested,
           t.avg_entry_price, t.basket_start_time, t.cycle_id
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE t.entry_confirmed=1 OR t.total_invested > 0 OR t.current_step > 0
    ORDER BY b.pair
""").fetchall()

for r in rows:
    print(f"  BotID={r[0]} {r[1]} {r[2]} base={r[3]} step={r[4]} confirmed={r[5]} "
          f"invested={r[6]:.2f} avg={r[7]:.4f} bst={r[8]} cycle={r[9]}")

print()
print('=== BOT_ORDERS PER IN-TRADE BOT ===')
for r in rows:
    bot_id, pair, direction, base_size = r[0], r[1], r[2], r[3]
    cycle_id = r[9]

    # Open/active orders
    open_orders = conn.execute("""
        SELECT order_id, order_type, step, price, amount, filled_amount, status, client_order_id
        FROM bot_orders
        WHERE bot_id=? AND status NOT IN ('cancelled','filled','closed','failed','auto_closed','reset_cleared')
        ORDER BY step
    """, (bot_id,)).fetchall()

    # All orders this cycle
    all_orders = conn.execute("""
        SELECT order_type, step, price, filled_amount, status, client_order_id
        FROM bot_orders WHERE bot_id=? AND cycle_id=?
        ORDER BY step, order_type
    """, (bot_id, cycle_id)).fetchall()

    print(f"\n--- Bot {bot_id} ({pair} {direction}) ---")
    print(f"    Open/active orders: {len(open_orders)}")
    for o in open_orders:
        cid = str(o[7] or '')[:35]
        print(f"      oid={str(o[0])[:20]} type={o[1]} step={o[2]} px={o[3]} amt={o[4]} filled={o[5]} status={o[6]}")
        print(f"           cid={cid}")

    print(f"    All orders this cycle={cycle_id} ({len(all_orders)} total):")
    for o in all_orders:
        cid = str(o[5] or '')[:40]
        print(f"      type={o[0]:<15} step={o[1]} px={o[2]} filled={o[3]} status={o[4]}")
        print(f"           cid={cid}")

conn.close()
print('\nDone.')
