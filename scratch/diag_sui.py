from engine.database import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%SUI%'")
print("Bots:", cur.fetchall())

cur.execute("SELECT id, bot_id, cycle_id, order_type, amount, filled_amount, status, client_order_id FROM bot_orders WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE '%SUI%') AND filled_amount > 0 AND (order_type LIKE 'hedge%' OR order_type LIKE 'hedgetp%')")
print("\nHedge Orders:", cur.fetchall())
