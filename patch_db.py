import sqlite3
import os

DB_PATH = 'crypto_bot.db'

def patch_db():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Check if column exists
        c.execute("PRAGMA table_info(bots)")
        columns = [row[1] for row in c.fetchall()]
        
        if 'status' not in columns:
            print("Adding 'status' column to bots table...")
            c.execute("ALTER TABLE bots ADD COLUMN status TEXT DEFAULT 'Stopped'")
            conn.commit()
            print("Column added successfully.")
        else:
            print("'status' column already exists.")
            
        conn.close()
    except Exception as e:
        print(f"Error patching DB: {e}")

if __name__ == "__main__":
    patch_db()
