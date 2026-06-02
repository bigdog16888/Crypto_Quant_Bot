import engine.database

conn = engine.database.get_connection()
row = conn.execute("SELECT pair, normalized_pair FROM bots WHERE id=100318").fetchone()
print("sui long_hedge pair:", row[0], "normalized_pair:", row[1])
