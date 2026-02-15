"""Check XAU - simpler"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Get trade_history columns
cur.execute("PRAGMA table_info(trade_history)")
cols = [r[1] for r in cur.fetchall()]
print("trade_history columns:", cols)

# Check Bot 44 history with correct columns
cur.execute("SELECT * FROM trade_history WHERE bot_id = 44 ORDER BY id DESC LIMIT 10")
rows = cur.fetchall()
print(f"\nBot 44 trade history ({len(rows)} rows):")
for r in rows:
    print(f"  {r}")

conn.close()
