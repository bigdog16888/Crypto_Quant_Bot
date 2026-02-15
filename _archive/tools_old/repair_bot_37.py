import sys
import os
import sqlite3

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def repair_bot(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if it exists first
    cursor.execute('SELECT bot_id FROM trades WHERE bot_id = ?', (bot_id,))
    if cursor.fetchone():
        print(f"Trade record for Bot {bot_id} already exists.")
        return

    print(f"Restoring missing trade record for Bot {bot_id}...")
    try:
        cursor.execute('INSERT INTO trades (bot_id) VALUES (?)', (bot_id,))
        conn.commit()
        print("Success! Trade record inserted.")
    except Exception as e:
        print(f"Error inserting record: {e}")

if __name__ == "__main__":
    repair_bot(37)
