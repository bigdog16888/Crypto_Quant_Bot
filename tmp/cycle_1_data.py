import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT id, order_type, amount, filled_amount, created_at FROM bot_orders WHERE bot_id=10018 AND cycle_id=1")
total = 0.0
for r in c.fetchall():
    print(r)
    d = float(r[3] or 0)
    if r[1] in ('entry', 'grid', 'adoption', 'adoption_add'):
        total += d
    elif r[1] in ('adoption_reduce', 'tp', 'close', 'sl', 'dust_close'):
        total -= d
        
print("Total Cycle 1:", total)
conn.close()
