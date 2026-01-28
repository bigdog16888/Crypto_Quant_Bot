import sqlite3
import os

db_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bots';")
    if not cursor.fetchone():
        print("Error: Table 'bots' not found.")
        exit(1)

    # Count IN TRADE bots
    cursor.execute("SELECT count(*) FROM bots WHERE total_invested > 0")
    in_trade_count = cursor.fetchone()[0]
    
    print(f"In-Trade Bots: {in_trade_count}")
    
    conn.close()
except Exception as e:
    print(f"Database error: {e}")
