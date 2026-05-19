import sqlite3
conn = sqlite3.connect('crypto_bot.db')
row = conn.execute(
    "SELECT COUNT(*) as row_count FROM exchange_order_audit"
).fetchone()
print(f"row_count={row[0]}, status=table exists")
cols = [c[1] for c in conn.execute('PRAGMA table_info(exchange_order_audit)').fetchall()]
print(f"columns ({len(cols)}): {cols}")
conn.close()
