import engine.database

conn = engine.database.get_connection()

q = """
SELECT b.id, b.name, b.direction, o.order_type, o.status, o.filled_amount, o.amount, o.price, o.step, o.cycle_id, o.client_order_id, datetime(o.created_at, 'unixepoch', 'localtime')
FROM bot_orders o
JOIN bots b ON o.bot_id = b.id
WHERE b.pair LIKE '%SUI%'
AND b.id != 10018
AND o.filled_amount > 0
AND o.status NOT IN ('reset_cleared', 'auto_closed')
ORDER BY o.created_at DESC;
"""
rows = conn.execute(q).fetchall()
print("--- Active filled orders for other SUI bots ---")
for r in rows:
    print(r)
