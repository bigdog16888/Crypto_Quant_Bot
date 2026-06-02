import engine.database

conn = engine.database.get_connection()

q = """
SELECT cycle_id, open_qty, wipe_wall_ts, position_side
FROM trades
WHERE bot_id = 100318
"""
row = conn.execute(q).fetchone()
print("cycle_id:", row[0], "type:", type(row[0]))
print("open_qty:", row[1], "type:", type(row[1]))
print("wipe_wall_ts:", row[2], "type:", type(row[2]))
print("position_side:", row[3], "type:", type(row[3]))
