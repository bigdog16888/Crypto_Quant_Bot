import engine.database

conn = engine.database.get_connection()
schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='active_positions'").fetchone()[0]
print(schema)
