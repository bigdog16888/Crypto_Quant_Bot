import sqlite3
conn = sqlite3.connect('crypto_bot.db')
with open('migration_002_exchange_gate.sql') as f:
    conn.executescript(f.read())
conn.commit()
# Verify table exists
rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_order_audit'").fetchall()
print(f"Table created: {rows}")
idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='exchange_order_audit'").fetchall()
print(f"Indexes: {[r[0] for r in idx]}")
cols = conn.execute("PRAGMA table_info(exchange_order_audit)").fetchall()
print(f"Columns ({len(cols)}): {[c[1] for c in cols]}")
conn.close()
print("migration_002 complete.")
