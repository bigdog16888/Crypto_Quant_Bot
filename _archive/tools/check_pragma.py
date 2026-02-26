import sqlite3
import os

DB_PATH = "crypto_bot.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("PRAGMA journal_mode;")
mode = cursor.fetchone()[0]
print(f"Journal Mode: {mode}")

cursor.execute("PRAGMA synchronous;")
sync = cursor.fetchone()[0]
print(f"Synchronous: {sync}")

cursor.execute("SELECT count(*) FROM active_positions;")
count = cursor.fetchone()[0]
print(f"Row Count: {count}")

conn.close()
