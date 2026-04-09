"""Check BTC detailed state"""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

print('=== BTC trades ===')
t = conn.execute("SELECT current_step, entry_confirmed, total_invested, avg_entry_price, cycle_id FROM trades WHERE bot_id=10016").fetchone()
print(f"step={t[0]} confirmed={t[1]} inv={t[2]} avg={t[3]} cycle={t[4]}")

print('\n=== BTC recent bot_orders ===')
orders = conn.execute("SELECT order_type, step, price, amount, filled_amount, status, cycle_id FROM bot_orders WHERE bot_id=10016 ORDER BY created_at DESC LIMIT 15").fetchall()
for o in orders:
    print(f"[{o[6]}] {o[0]} step {o[1]} px {o[2]} amt {o[3]} filled {o[4]} status={o[5]}")

conn.close()
