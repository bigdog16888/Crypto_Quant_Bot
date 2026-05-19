import sqlite3
import os

db_path = "crypto_bot.db"
migration_path = "migration_001_wipe_proof.sql"

if not os.path.exists(db_path):
    print(f"Error: {db_path} not found.")
    exit(1)

if not os.path.exists(migration_path):
    print(f"Error: {migration_path} not found.")
    exit(1)

with open(migration_path, "r") as f:
    sql = f.read()

conn = sqlite3.connect(db_path)
try:
    conn.executescript(sql)
    conn.commit()
    print("Migration successful.")
except Exception as e:
    print(f"Migration failed: {e}")
    conn.rollback()
finally:
    conn.close()
