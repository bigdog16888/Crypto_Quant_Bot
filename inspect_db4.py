import sqlite3
conn = sqlite3.connect('crypto_bot.db.backup_20260807_153057')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
for row in cursor.fetchall():
    print(row)