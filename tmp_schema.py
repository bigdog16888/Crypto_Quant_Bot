import sqlite3

db_path = 'quant_bot.db'
conn = sqlite3.connect(db_path)
c = conn.cursor()

# Get table names
tables = [r[1] for r in c.execute("SELECT * FROM sqlite_master WHERE type='table'")]
print("Tables:", tables)

# Find trade state table
for t in tables:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    print(f"  {t}: {cols}")
