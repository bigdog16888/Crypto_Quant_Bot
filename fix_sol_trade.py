# Clear the SOL bot trade that has a stale $1576 investment from a previous cycle
# The real exchange position is only $79 SHORT, so this trade row is stale
import sqlite3
conn = sqlite3.connect('crypto_bot.db')

# Check what's there first  
rows = conn.execute("SELECT bot_id, total_invested, current_step, basket_start_time FROM trades WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE '%SOL%')").fetchall()
print("SOL trades before:", rows)

# Clear the stale SOL trade (Bot 10008)
conn.execute("DELETE FROM trades WHERE bot_id = 10008")
conn.execute("UPDATE bots SET status='Scanning' WHERE id = 10008")
conn.execute("UPDATE bot_orders SET status='reset_cleared', updated_at=strftime('%s','now') WHERE bot_id=10008 AND status IN ('open','filled','closed','missing')")
conn.commit()

print("SOL trades after:", conn.execute("SELECT bot_id, total_invested FROM trades WHERE bot_id=10008").fetchall())
print("Bot 10008 status:", conn.execute("SELECT status FROM bots WHERE id=10008").fetchone())
print("Done. SOL bot reset to Scanning.")
