
import sqlite3
import os
import sys

sys.path.append(os.getcwd())
from config.settings import config

def check_owners(order_ids):
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"--- Checking Owners for IDs: {order_ids} ---")
    
    # Check trade_history
    cursor.execute(f"SELECT * FROM trade_history WHERE order_id IN ({','.join(['?']*len(order_ids))})", order_ids)
    rows = cursor.fetchall()
    if rows:
        for r in rows:
            print(f"HISTORY: Bot {r[0]} | Action {r[1]} | OrderID {r[6]} | Notes {r[8]}")
            
    # Check trades table (active)
    cursor.execute(f"SELECT * FROM trades WHERE order_id IN ({','.join(['?']*len(order_ids))})", order_ids)
    rows = cursor.fetchall()
    if rows:
        for r in rows:
             print(f"ACTIVE TRADE: Bot {r[0]} | OrderID {r[5]}")
             
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        ids = sys.argv[1:]
        check_owners(ids)
    else:
        print("Usage: python find_order_owner.py ID1 ID2 ...")
