from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()
bot_id = 10022
cur.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,))
cycle_id = cur.fetchone()[0]
print(f"Bot {bot_id} is in Cycle {cycle_id}")

cur.execute("SELECT id, cycle_id, order_type, amount, filled_amount, status, client_order_id, created_at FROM bot_orders WHERE bot_id=? AND status NOT IN ('canceled', 'cancelled', 'rejected') ORDER BY id DESC LIMIT 20", (bot_id,))
print("\nRecent Orders:")
for o in cur.fetchall():
    print(f"  {o}")
