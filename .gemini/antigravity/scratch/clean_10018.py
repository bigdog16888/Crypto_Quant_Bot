import sqlite3
import time
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
# Mark all currently filled orders for 10018 as reset_cleared to wipe the zombie slate clean
cur.execute("UPDATE bot_orders SET status='reset_cleared' WHERE bot_id=10018")
# Reset the trades table
cur.execute("UPDATE trades SET open_qty=0, total_invested=0, avg_entry_price=0, current_step=0, entry_confirmed=0, wipe_wall_ts=0, cycle_start_time=0 WHERE bot_id=10018")
conn.commit()
conn.close()
print('Cleaned Bot 10018 zombie slate. StateReconciler will now trigger Directional Gap Adoption.')
