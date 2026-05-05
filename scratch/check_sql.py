from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT id, status FROM bot_orders WHERE bot_id=10017 AND cycle_id=19 AND status NOT IN ('auto_closed', 'reset_cleared', 'canceled', 'cancelled', 'rejected')")
print("Filtered Orders:", cur.fetchall())
