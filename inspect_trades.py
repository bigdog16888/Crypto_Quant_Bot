import sqlite3
from pathlib import Path

db_path = Path("crypto_bot.db")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

output = []
tables = ["trades"]

for table in tables:
    output.append(f"--- {table} ---")
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = cursor.fetchall()
        for col in columns:
            output.append(str(col))
    except Exception as e:
        output.append(f"Error: {e}")
    output.append("")

conn.close()

with open("schema_dump_trades.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(output))
