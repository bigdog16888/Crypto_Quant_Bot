import sys
import os
import sqlite3

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from engine.database import get_connection

def verify_bots():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, is_active FROM bots WHERE name IN ('Test_Bot_A', 'Test_Bot_B', 'Test_Bot_C')")
    rows = cursor.fetchall()
    
    print("Bot Status:")
    for row in rows:
        print(f"Bot {row[0]}: {row[1]} - Active: {row[2]}")
        if row[2] != 1:
            print(f"Warning: Bot {row[1]} is NOT active.")

if __name__ == "__main__":
    verify_bots()
