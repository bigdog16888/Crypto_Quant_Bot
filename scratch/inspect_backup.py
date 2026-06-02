import sqlite3
import json

def inspect_backup():
    db_path = 'backups/crypto_bot_backup_20260527_135336.db'
    print(f"Querying backup: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # Get trades
    cur.execute("SELECT * FROM trades WHERE bot_id = 10018")
    trade = cur.fetchone()
    if trade:
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        print("\nTrade Info:")
        for col, val in zip(cols, trade):
            print(f"  {col}: {val}")
            
    # Get bot orders
    cur.execute("SELECT id, order_id, order_type, price, amount, filled_amount, status, step, cycle_id FROM bot_orders WHERE bot_id = 10018 ORDER BY created_at DESC LIMIT 10")
    orders = cur.fetchall()
    print("\nOrders:")
    for o in orders:
        print(f"  ID: {o[0]} | OID: {o[1]} | Type: {o[2]} | Price: {o[3]} | Qty: {o[4]} | Filled: {o[5]} | Status: {o[6]} | Step: {o[7]} | CycleID: {o[8]}")
        
    conn.close()

if __name__ == '__main__':
    inspect_backup()
