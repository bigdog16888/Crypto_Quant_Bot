import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute("SELECT id, status, wipe_proof_source FROM bot_orders WHERE status = 'reset_cleared' LIMIT 10")
rows = cursor.fetchall()
for r in rows:
    print(r)
conn.close()
