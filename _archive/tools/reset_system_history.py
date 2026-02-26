
import sqlite3
import os
import time

DB_PATH = 'crypto_bot.db'

def reset_system_history():
    print("--- 🚨 FACTORY RESET: Wiping History (Keeping Bots) 🚨 ---")
    
    # 1. Connect
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 2. Lists of tables to truncate (or drop/recreate structure if sqlite doesn't support truncate)
    # SQLite doesn't have TRUNCATE, so we DELETE FROM.
    tables_to_wipe = [
        'orders',
        'trades',
        'signals',
        'system_logs', 
        'trade_approvals'
    ]

    try:
        cursor.execute("BEGIN TRANSACTION;")
        
        for table in tables_to_wipe:
            # Check if table exists first
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}';")
            if cursor.fetchone():
                print(f"🧹 Wiping table: {table}...")
                cursor.execute(f"DELETE FROM {table};")
                # Reset autoincrement
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}';")
            else:
                print(f"⚠️ Table {table} not found, skipping.")

        # 3. Reset Bot State (But keep the bots)
        # Set all bots to IDLE, no errors, no open orders/positions tracked in DB field
        print("🔄 Resetting active bots to IDLE state...")
        cursor.execute("""
            UPDATE bots 
            SET status = 'IDLE',
                is_active = 0,
                total_invested = 0.0,
                current_position = 0.0,
                avg_price = 0.0,
                unrealized_pnl = 0.0,
                error_count = 0,
                last_error = NULL
        """)

        conn.commit()
        print("✅ SUCCESS: System history wiped. Bots are now clean and IDLE.")
        print("👉 Please manually delete 'engine.log' if you want a fresh log file.")

    except Exception as e:
        conn.rollback()
        print(f"❌ ERROR: Failed to reset system. {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    confirmation = input("Type 'RESET' to confirm wiping all history: ")
    if confirmation == 'RESET':
        reset_system_history()
    else:
        print("❌ Action cancelled.")
