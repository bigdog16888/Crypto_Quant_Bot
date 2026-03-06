import sqlite3
import json

db_path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- BOT 10010 INFO ---")
cursor.execute("SELECT id, name, pair, direction, config FROM bots WHERE id=10010")
row = cursor.fetchone()
if row:
    print(f"ID: {row[0]}, Name: {row[1]}, Pair: {row[2]}, Direction: {row[3]}")
    try:
        cfg = json.loads(row[4])
        print("Config:")
        print(json.dumps(cfg, indent=2))
    except Exception as e:
        print("Error parsing config:", e)
else:
    print("Bot 10010 not found.")

print("\n--- ACTIVE POSITIONS ---")
cursor.execute("SELECT pair, side, size, entry_price FROM active_positions WHERE pair LIKE '%XRP%'")
for r in cursor.fetchall():
    print(r)

print("\n--- RECONCILIATION LOGS ---")
cursor.execute("SELECT timestamp, action, details, bot_id FROM reconciliation_logs WHERE pair LIKE '%XRP%' ORDER BY id DESC LIMIT 10")
for r in cursor.fetchall():
    print(r)

conn.close()
