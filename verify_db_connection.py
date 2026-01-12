import sqlite3
import os

DB_PATH = "crypto_bot.db"

print(f"Current working directory: {os.getcwd()}")
print(f"Expected DB path: {os.path.abspath(DB_PATH)}")

if os.path.exists(DB_PATH):
    print("DB file exists.")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print("Connection successful.")
        print("Tables:", tables)
        conn.close()
    except Exception as e:
        print(f"Failed to connect: {e}")
else:
    print("DB file does not exist at expected path.")
    # Try to create it to see if permissions work
    try:
        conn = sqlite3.connect(DB_PATH)
        print("Created new DB file successfully.")
        conn.close()
        os.remove(DB_PATH) # Cleanup
    except Exception as e:
        print(f"Failed to create DB: {e}")
