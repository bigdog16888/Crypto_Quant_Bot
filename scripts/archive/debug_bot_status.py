import sys
import os
import sqlite3
import json

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def debug_bot(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check Bots Table
    cursor.execute('SELECT id, name, is_active, pair FROM bots WHERE id = ?', (bot_id,))
    row = cursor.fetchone()
    if not row:
        print("Bot not found")
        return
    print(f"Bot: {row[1]} (ID: {row[0]})")
    print(f"Active: {row[2]}")
    print(f"Pair: {row[3]}")
    
    # Check Trades Table
    cursor.execute('SELECT current_step, total_invested, basket_start_time FROM trades WHERE bot_id = ?', (bot_id,))
    t_row = cursor.fetchone()
    if t_row:
        print(f"Current Step: {t_row[0]}")
        print(f"Total Invested: {t_row[1]}")
        print(f"Basket Start: {t_row[2]}")
        
        if t_row[1] > 0:
            print("STATUS: IN TRADE (Invested > 0)")
        else:
            print("STATUS: NOT IN TRADE")
    else:
        print("No Trade Record Found!")

if __name__ == "__main__":
    debug_bot(37)
