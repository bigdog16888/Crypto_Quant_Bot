import sqlite3
import os

db_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Count IN TRADE bots using trades table
    cursor.execute("SELECT count(*) FROM trades WHERE total_invested > 0")
    result = cursor.fetchone()
    in_trade_count = result[0] if result else 0
    
    print(f"In-Trade Bots: {in_trade_count}")
    
    conn.close()
except Exception as e:
    print(f"Database error: {e}")
