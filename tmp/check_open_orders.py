"""Check bot orders"""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
bots = [10016, 10008, 10019]
print('=== BOT STATE ===')
for b in bots:
    t = conn.execute('SELECT b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price, t.cycle_id FROM trades t JOIN bots b ON b.id=t.bot_id WHERE t.bot_id=?', (b,)).fetchone()
    if t: print(f'Bot {b} {t[0]} {t[1]}: step={t[2]} inv=${t[3]:.2f} avg={t[4]:.4f} cycle={t[5]}')
    
print('\n=== OPEN ORDERS ===')
for b in bots:
    orders = conn.execute("SELECT order_type, step, price, amount FROM bot_orders WHERE bot_id=? AND status IN ('open','new','placing') ORDER BY step, order_type", (b,)).fetchall()
    print(f'Bot {b}: {len(orders)} open limit orders')
    for o in orders: print(f'  {o[0]} step {o[1]} px {o[2]} amt {o[3]}')
conn.close()
