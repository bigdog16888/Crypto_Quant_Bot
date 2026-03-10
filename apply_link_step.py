import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

bot_id = 10020

# 1. Recover the true Step (max step since last entry)
c.execute("""
    SELECT MAX(step) FROM bot_orders 
    WHERE bot_id = ? AND status IN ('filled', 'closed', 'open') 
    AND order_type IN ('entry', 'grid')
    AND created_at >= COALESCE((
        SELECT MAX(created_at) FROM bot_orders 
        WHERE bot_id = ? AND order_type = 'entry' AND status IN ('filled', 'closed')
    ), 0)
""", (bot_id, bot_id))
row = c.fetchone()
true_step = row[0] if row and row[0] else 1

print(f"✅ Recovered True Step for LINK: {true_step}")

# 2. Recover the Basket Start Time
# This is the timestamp of the last 'entry' order.
c.execute("""
    SELECT MAX(created_at) FROM bot_orders 
    WHERE bot_id = ? AND order_type = 'entry' AND status IN ('filled', 'closed')
""", (bot_id,))
row2 = c.fetchone()
basket_start = row2[0] if row2 and row2[0] else None

if basket_start:
    print(f"✅ Recovered Basket Start Time: {basket_start}")

# 3. Apply corrections
c.execute("UPDATE trades SET current_step=? WHERE bot_id=?", (true_step, bot_id))
if basket_start:
    c.execute("UPDATE trades SET basket_start_time=? WHERE bot_id=?", (basket_start, bot_id))

# 4. Check actual DB values
c.execute("SELECT current_step, basket_start_time, total_invested, avg_entry_price FROM trades WHERE bot_id=?", (bot_id,))
print("Updated Trade state:", c.fetchone())

conn.commit()
conn.close()
