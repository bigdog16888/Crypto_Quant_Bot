import sqlite3
import json

db_path = "crypto_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("--- ETH Bot Configs Raw ---")
rows = cur.execute("SELECT id, name, config FROM bots WHERE pair LIKE 'ETH%USDC%';").fetchall()
for r in rows:
    bid, name, cfg_str = r
    print(f"\nBot {bid} ({name}):")
    print(cfg_str)

conn.close()
