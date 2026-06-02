import engine.database

conn = engine.database.get_connection()

print("--- All bots for SUI ---")
q = """
SELECT b.id, b.name, b.direction, b.is_active, b.status, t.open_qty, t.total_invested, t.avg_entry_price, t.cycle_id
FROM bots b
LEFT JOIN trades t ON t.bot_id = b.id
WHERE b.pair LIKE '%SUI%'
"""
rows = conn.execute(q).fetchall()
for r in rows:
    print(r)
