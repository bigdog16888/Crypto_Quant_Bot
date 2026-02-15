import sqlite3
import os
import sys
import glob

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection

def purge_system():
    print("🔥 INITIATING TOTAL SYSTEM PURGE 🔥")
    print("===================================")
    
    # 1. Database Purge
    print("1. Truncating Database Tables...")
    conn = get_connection()
    cursor = conn.cursor()
    
    tables = ['trades', 'bot_orders', 'bot_ownership_state', 'notifications', 'trade_history']
    for table in tables:
        try:
            cursor.execute(f"DELETE FROM {table}")
            print(f"   ✅ Cleared table: {table}")
        except Exception as e:
            print(f"   ⚠️ Error clearing {table}: {e}")
            
    conn.commit()
    conn.close()
    
    # 2. Log File Purge
    print("\n2. Deleting Log Files...")
    log_files = glob.glob("*.log")
    for log in log_files:
        try:
            os.remove(log)
            print(f"   ✅ Deleted log: {log}")
        except Exception as e:
            print(f"   ⚠️ Could not delete {log}: {e}")
            
    print("\n✅ PURGE COMPLETE. SYSTEM IS TABULA RASA.")
    print("===================================")

if __name__ == "__main__":
    purge_system()
