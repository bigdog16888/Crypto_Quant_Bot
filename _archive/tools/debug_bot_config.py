
import sqlite3
import os
import sys

# Add root to sys.path
sys.path.append(os.getcwd())

from config.settings import config

def list_all_bots():
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"--- Listing All Bots in {db_path} ---")
    
    cursor.execute("SELECT id, name, pair, direction, is_active FROM bots")
    bots = cursor.fetchall()
    
    if bots:
        print(f"Found {len(bots)} bots:")
        for b in bots:
            print(f"  ID: {b[0]} | Name: {b[1]} | Pair: {b[2]} | Dir: {b[3]} | Active: {b[4]}")
    else:
        print("No bots found in 'bots' table.")

    print("\n--- Listing All Trades ---")
    cursor.execute("SELECT bot_id, total_invested, current_step FROM trades")
    trades = cursor.fetchall()
    if trades:
        for t in trades:
            print(f"  BotID: {t[0]} | Invested: {t[1]} | Step: {t[2]}")
    else:
        print("No trades found.")

    conn.close()

if __name__ == "__main__":
    list_all_bots()
