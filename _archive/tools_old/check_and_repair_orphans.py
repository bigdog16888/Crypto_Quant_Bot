import sys
import os
import sqlite3

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def check_and_repair():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("Checking for orphaned bots (Bots without Trade records)...")
    
    cursor.execute('''
        SELECT b.id, b.name 
        FROM bots b 
        LEFT JOIN trades t ON b.id = t.bot_id 
        WHERE t.bot_id IS NULL
    ''')
    orphans = cursor.fetchall()
    
    if not orphans:
        print("No orphaned bots found.")
        return
        
    print(f"Found {len(orphans)} orphaned bots:")
    for b in orphans:
        print(f" - ID: {b[0]} | Name: {b[1]}")
        
    # Repair
    print("\nRepairing...")
    for b in orphans:
        try:
            print(f"Restoring trade record for Bot {b[0]} ({b[1]})...")
            cursor.execute('INSERT INTO trades (bot_id) VALUES (?)', (b[0],))
            conn.commit()
            print("Success.")
        except Exception as e:
            print(f"Failed: {e}")

if __name__ == "__main__":
    check_and_repair()
