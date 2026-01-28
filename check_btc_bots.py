import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Get all BTC/USDC bots
cur.execute("SELECT id, name, pair, is_active FROM bots WHERE pair LIKE '%BTC%USDC%' ORDER BY id DESC")
rows = cur.fetchall()

print("BTC/USDC Bots:")
for r in rows:
    print(f"  Bot #{r[0]}: {r[1]} - Active: {'YES' if r[3] else 'NO'}")

conn.close()
