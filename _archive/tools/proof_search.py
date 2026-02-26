
import sqlite3
import os
import sys

sys.path.append(os.getcwd())
from config.settings import config
from engine.exchange_interface import ExchangeInterface

def find_cleanup_proof():
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("\n--- Searching for Cleanup Actions ---")
    cursor.execute("SELECT * FROM trade_history WHERE notes LIKE '%Rogue%' OR notes LIKE '%Closing%' ORDER BY timestamp DESC")
    rows = cursor.fetchall()
    if rows:
        for r in rows:
            print(f"Action: {r[1]} | Bot: {r[0]} | Amt: {r[4]} | OrderID: {r[6]} | Notes: {r[8]} | TS: {r[9]}")
    else:
        print("No 'Rogue' or 'Closing' notes found in DB.")

    print("\n--- Searching for Exchange Market Orders (ReduceOnly) ---")
    try:
        interface = ExchangeInterface()
        orders = interface.client.fetch_closed_orders('BTC/USDC', limit=50)
        # Look for MARKET orders that are 'reduceOnly' or just recent market orders around the fix time
        # Fix time was roughly 1771258500 - 1771258800
        
        found = []
        for o in orders:
            # We want Market orders
            if o['type'].lower() == 'market':
                print(f"Market Order: {o['id']} | Side: {o['side']} | Amt: {o['amount']} | TS: {o['timestamp']}")
                found.append(o)
        
        if not found:
            print("No market orders found recently.")
            
    except Exception as e:
        print(f"Exchange error: {e}")

    conn.close()

if __name__ == "__main__":
    find_cleanup_proof()
