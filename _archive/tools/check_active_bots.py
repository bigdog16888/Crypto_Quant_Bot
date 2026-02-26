import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from config.settings import config
from engine.database import DB_PATH

def dump_active_bots():
    print(f"Connecting to DB at {DB_PATH}")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT b.id, b.name, b.pair, b.strategy_type, t.total_invested FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1")
        rows = cursor.fetchall()
        print("--- ACTIVE BOTS ---")
        for row in rows:
            print(f"ID: {row[0]} | Name: {row[1]} | Pair: {row[2]} | Type: {row[3]} | Total Invested: {row[4]}")
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    dump_active_bots()
