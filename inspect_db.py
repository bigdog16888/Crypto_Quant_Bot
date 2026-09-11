import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(bot_orders)')
for row in cursor.fetchall():
    print(row)