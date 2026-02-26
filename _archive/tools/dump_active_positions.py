
import sqlite3
import time
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import config

DB_PATH = config.PATHS['DB_FILE']

def dump_positions():
    print(f"--- DUMPING ACTIVE POSITIONS FROM {DB_PATH} ---")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM active_positions")
        rows = cursor.fetchall()
        print(f"Row Count: {len(rows)}")
        for row in rows:
            print(row)
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    dump_positions()
