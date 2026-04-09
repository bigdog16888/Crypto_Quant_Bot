"""Check bot_orders cycle_id distribution for BTC/SOL/Gold."""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection
conn = get_connection()
for bot_id, label in [(10016,'BTC LONG'),(10008,'SOL LONG'),(10019,'XAU SHORT')]:
    print(f'\n--- Bot {bot_id} {label} ---')
    # All orders by cycle_id
    r = conn.execute('SELECT COUNT(*), cycle_id FROM bot_orders WHERE bot_id=? GROUP BY cycle_id ORDER BY cycle_id', (bot_id,)).fetchall()
    for cnt, cid in r:
        print(f'  cycle_id={cid}: {cnt} orders')
    # Filled entry/grid orders specifically
    filled_q = """
        SELECT COUNT(*), cycle_id FROM bot_orders
        WHERE bot_id=? AND filled_amount>0
          AND order_type IN ('entry','grid','adoption')
          AND status IN ('filled','closed','partially_filled')
        GROUP BY cycle_id
    """
    filled = conn.execute(filled_q, (bot_id,)).fetchall()
    print(f'  Filled entry/grid by cycle_id: {filled if filled else "NONE"}')
    # Sample filled entries
    sample = conn.execute("""
        SELECT order_type, step, price, filled_amount, status, cycle_id, client_order_id
        FROM bot_orders WHERE bot_id=? AND filled_amount>0
          AND order_type IN ('entry','grid','adoption')
        LIMIT 5
    """, (bot_id,)).fetchall()
    for s in sample:
        print(f'    type={s[0]} step={s[1]} px={s[2]} fill={s[3]} status={s[4]} cycle={s[5]} cid={str(s[6])[:30]}')
conn.close()
print('\nDone.')
