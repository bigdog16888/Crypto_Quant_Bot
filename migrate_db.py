import sqlite3
import os
import sys

# Ensure engine can be imported
sys.path.append(os.getcwd())
from engine.database import DB_PATH

def migrate():
    print(f"Migrating database at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check for config column
    try:
        cursor.execute("SELECT config FROM bots LIMIT 1")
        print("  - Column 'config' already exists.")
    except sqlite3.OperationalError:
        print("  - Column 'config' missing. Adding it...")
        cursor.execute("ALTER TABLE bots ADD COLUMN config TEXT DEFAULT '{}'")
        conn.commit()
        print("  - Column 'config' added successfully.")
        
    conn.close()

if __name__ == "__main__":
    migrate()
