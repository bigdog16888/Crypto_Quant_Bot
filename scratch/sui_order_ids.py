import engine.database

conn = engine.database.get_connection()

q = """
SELECT id, order_id, client_order_id, order_type, step, price, amount, filled_amount, status, datetime(created_at, 'unixepoch', 'localtime')
FROM bot_orders
WHERE bot_id = (SELECT id FROM bots WHERE name = 'sui long')
AND cycle_id = 87
AND filled_amount > 0
ORDER BY step ASC;
"""
rows = conn.execute(q).fetchall()
print("--- Filled orders in cycle 87 for sui long ---")
for r in rows:
    print(r)
