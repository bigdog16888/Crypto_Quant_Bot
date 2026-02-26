
import sqlite3
import os
import sys

# Add root to sys.path
sys.path.append(os.getcwd())

from config.settings import config

def check_bot_10000():
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"--- Checking Bot 10000 in {db_path} ---")
    
    cursor.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE id=10000")
    bot = cursor.fetchone()
    if bot:
        print(f"Bot 10000: Name={bot[1]} | Pair={bot[2]} | Dir={bot[3]} | Active={bot[4]}")
    else:
        print("Bot 10000 not found.")
        
    cursor.execute("SELECT total_invested FROM trades WHERE bot_id=10000")
    trade = cursor.fetchone()
    if trade:
        print(f"Trade Invested: {trade[0]}")

    conn.close()

if __name__ == "__main__":
    check_bot_10000()
