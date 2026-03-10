
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "crypto_bot.db")

def full_db_audit():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("--- Database Health Audit ---")
    
    # 1. Active Trades
    cursor.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
    active_trades = cursor.fetchone()[0]
    print(f"Active Trades (> $0): {active_trades}")
    
    # 2. Bot Statuses
    cursor.execute("SELECT COUNT(*) FROM bots WHERE status != 'Scanning' AND is_active = 1")
    not_scanning = cursor.fetchone()[0]
    print(f"Bots NOT in 'Scanning' state: {not_scanning}")
    
    # 3. Open Orders
    cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE status IN ('open', 'pending')")
    open_orders = cursor.fetchone()[0]
    print(f"Open Orders in DB: {open_orders}")
    
    # 4. Total Invested Sum
    cursor.execute("SELECT SUM(total_invested) FROM trades")
    total_inv = cursor.fetchone()[0] or 0
    print(f"Total Invested (Sum): ${total_inv:.2f}")

    if active_trades == 0 and not_scanning == 0 and open_orders == 0:
        print("\n✨ DATABASE IS 100% CLEAN.")
    else:
        print("\n⚠️ DATABASE STILL HAS RECORDS.")
        
    conn.close()

if __name__ == "__main__":
    find_link_bot()
