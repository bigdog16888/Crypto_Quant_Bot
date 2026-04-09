import sqlite3
import os
import sys

# ADD CWD to sys path
sys.path.append(os.getcwd())

def clean():
    # The actual DB_PATH is in engine.database
    from engine.database import DB_PATH
    
    print(f"Connecting to database: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database file {DB_PATH} not found.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # 1. Delete all non-deterministic PASS3 orders
    cur.execute("DELETE FROM bot_orders WHERE order_id LIKE 'PASS3_ORPHAN_%'")
    deleted_orphans = cur.rowcount
    
    # 2. Delete all new deterministic PASS3 orders so Reconciler can generate a fresh accurate gap injection
    cur.execute("DELETE FROM bot_orders WHERE order_id LIKE 'PASS3_ADOPTION_%'")
    deleted_adoptions = cur.rowcount
    
    conn.commit()
    conn.close()
    
    print(f"Purged {deleted_orphans} old PASS3 orphans and {deleted_adoptions} new PASS3 adoptions.")

if __name__ == "__main__":
    clean()
