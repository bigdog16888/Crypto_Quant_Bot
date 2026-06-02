import engine.database

conn = engine.database.get_connection()

q = """
SELECT id, client_order_id, position_side, order_type, filled_amount, status, cycle_id
FROM bot_orders
WHERE bot_id = 100318
"""
rows = conn.execute(q).fetchall()
print("--- bot_orders for sui long_hedge (100318) ---")
for r in rows:
    print(r)
