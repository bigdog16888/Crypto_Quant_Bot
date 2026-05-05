from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT id, status, cycle_id, order_type FROM bot_orders WHERE bot_id=100000 AND order_type LIKE 'hedge%'")
print("Hedge Orders:")
for o in cur.fetchall():
    print(o)
