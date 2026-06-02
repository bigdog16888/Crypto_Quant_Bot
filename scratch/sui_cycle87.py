import engine.database

conn = engine.database.get_connection()

q = """
SELECT order_type, status, filled_amount, amount, price, step, cycle_id, datetime(created_at, 'unixepoch', 'localtime'), client_order_id, notes
FROM bot_orders
WHERE bot_id = (SELECT id FROM bots WHERE name = 'sui long')
AND cycle_id = 87
ORDER BY created_at ASC;
"""
rows = conn.execute(q).fetchall()
print("--- All Cycle 87 orders for sui long ---")
total_filled = 0.0
for r in rows:
    print(r)
    otype = r[0]
    filled = float(r[2] or 0)
    status = r[1]
    if status not in ('cancelled', 'canceled', 'failed', 'rejected', 'auto_closed', 'reset_cleared'):
        # For active cycle recomputation
        if otype in ('entry', 'grid', 'adoption_add', 'adoption', 'forensic_adoption_add'):
            total_filled += filled
        elif otype in ('tp', 'close', 'sl', 'dust_close', 'adoption_reduce', 'forensic_adoption_reduce'):
            total_filled -= filled
print("Sum of active fills in cycle 87:", total_filled)
