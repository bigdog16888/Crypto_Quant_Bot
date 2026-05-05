import sqlite3
import os

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- ACTIVE BOTS ---")
cursor.execute("SELECT id, name, pair, normalized_pair, direction, is_active FROM bots WHERE is_active=1")
for row in cursor.fetchall():
    print(row)

print("\n--- ACTIVE POSITIONS ---")
cursor.execute("SELECT bot_id, pair, side, size FROM active_positions")
for row in cursor.fetchall():
    print(row)

print("\n--- TRADES TABLE (IN TRADE) ---")
cursor.execute("SELECT bot_id, total_invested, open_qty, cycle_id, cycle_phase FROM trades WHERE total_invested > 0 OR open_qty > 0")
for row in cursor.fetchall():
    print(row)

conn.close()
