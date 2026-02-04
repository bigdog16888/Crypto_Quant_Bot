import sys
import os
import sqlite3

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def list_bots():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, is_active FROM bots')
    rows = cursor.fetchall()
    print(f"{'ID':<5} {'Name':<30} {'Active':<10}")
    print("-" * 50)
    for r in rows:
        print(f"{r[0]:<5} {r[1]:<30} {r[2]:<10}")

if __name__ == "__main__":
    list_bots()
