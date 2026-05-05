from engine.database import sync_trades_from_orders, get_connection
import logging

logging.basicConfig(level=logging.INFO)

bot_id = 100000
print(f"Force Syncing Bot {bot_id}...")
sync_trades_from_orders(bot_id)

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT hedge_qty, open_qty FROM trades WHERE bot_id=?", (bot_id,))
res = cur.fetchone()
print(f"Result in trades table: {res}")
