
import sqlite3
import os
import sys
import time

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.database import init_db, get_connection, DB_PATH, BASE_DIR

def verify_safety():
    print("--- VERIFYING DB SAFETY FIXES ---")
    
    # 1. Trigger init_db (should create backup)
    print("1. Running init_db()...")
    init_db()
    
    # 2. Check Backup
    backup_dir = os.path.join(BASE_DIR, "backups")
    if os.path.exists(backup_dir):
        files = os.listdir(backup_dir)
        backups = [f for f in files if f.endswith('.db')]
        if backups:
            print(f"✅ Backup created: {backups[-1]}")
        else:
            print("❌ No backup file found in backups/ directory!")
    else:
        print("❌ backups/ directory missing!")

    # 3. Check WAL Mode
    print("2. Checking Journal Mode...")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    
    if mode.upper() == 'WAL':
        print(f"✅ Journal Mode is WAL.")
    else:
        print(f"❌ Journal Mode is {mode} (Expected WAL)")
        
    print("--- VERIFICATION DONE ---")

if __name__ == "__main__":
    verify_safety()
