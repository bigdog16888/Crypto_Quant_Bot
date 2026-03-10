import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

bot_id = 10020
c.execute("""
    SELECT MAX(step) FROM bot_orders 
    WHERE bot_id = ? AND status IN ('filled', 'closed', 'open') 
    AND cycle_id = (SELECT cycle_id FROM bots WHERE id=?)
    AND order_type IN ('entry', 'grid')
""", (bot_id, bot_id))
row = c.fetchone()
calc_step = row[0] if row and row[0] else 1

print(f"Recovered Step: {calc_step}")

# Fix the current step in the DB for the user
c.execute("UPDATE trades SET current_step=? WHERE bot_id=?", (calc_step, bot_id))

# Optionally, rewrite basket_start_time so EE doesn't restart completely if we know the first order time
c.execute("SELECT MIN(created_at) FROM bot_orders WHERE bot_id=? AND cycle_id=(SELECT cycle_id FROM bots WHERE id=?) AND status='filled'", (bot_id, bot_id))
time_row = c.fetchone()
if time_row and time_row[0]:
    c.execute("UPDATE trades SET basket_start_time=? WHERE bot_id=?", (time_row[0], bot_id))
    print(f"Recovered Basket Start Time: {time_row[0]}")

conn.commit()
print("Applied corrections to LINK bot.")
