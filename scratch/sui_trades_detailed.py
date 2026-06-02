import engine.database

conn = engine.database.get_connection()
cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
row = conn.execute("SELECT * FROM trades WHERE bot_id=100318").fetchone()

print("--- trades table columns and values for 100318 ---")
for col, val in zip(cols, row):
    print(f"{col}: {val} (type: {type(val)})")
