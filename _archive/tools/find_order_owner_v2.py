
import sqlite3
import os
import sys

sys.path.append(os.getcwd())
try:
    from config.settings import config
except ImportError:
    # Fallback if config is missing (unlikely)
    class Config:
        PATHS = {"DB_FILE": "crypto_bot.db"}
    config = Config()

def check_schema_and_owners(order_ids):
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    # Use row factory for easier access
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("--- TRADES Schema ---")
    cursor.execute("PRAGMA table_info(trades)")
    cols = [r['name'] for r in cursor.fetchall()]
    print(f"Columns: {cols}")
    
    print(f"\n--- Checking Owners for IDs: {order_ids} ---")
    
    # Check trade_history (confirmed has order_id)
    try:
        # Construct query carefully
        placeholders = ','.join(['?']*len(order_ids))
        query = f"SELECT * FROM trade_history WHERE order_id IN ({placeholders})"
        cursor.execute(query, order_ids)
        rows = cursor.fetchall()
        for r in rows:
            print(f"HISTORY: Bot {r['bot_id']} | Action {r['action']} | OrderID {r['order_id']} | Notes {r['notes']}")
    except Exception as e:
        print(f"History Check Error: {e}")

    # Check trades table
    try:
        # Search all likely columns
        potential_cols = ['current_order_id', 'last_order_id', 'entry_order_id', 'tp_order_id', 'sl_order_id']
        found_match = False
        
        for col in cols:
            if 'order' in col and 'id' in col:
                query = f"SELECT * FROM trades WHERE {col} IN ({placeholders})"
                cursor.execute(query, order_ids)
                rows = cursor.fetchall()
                for r in rows:
                    print(f"ACTIVE TRADE (via {col}): Bot {r['bot_id']}")
                    found_match = True
                    
        if not found_match:
            print("No matches in active trades table.")
            
    except Exception as e:
        print(f"Trades Check Error: {e}")
             
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        ids = sys.argv[1:]
        check_schema_and_owners(ids)
    else:
        # Default test if no args
        print("Usage: python find_order_owner_v2.py ID1 ID2 ...")
