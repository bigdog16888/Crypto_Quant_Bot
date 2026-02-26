
import sqlite3
import os
import sys
import time
import json
from datetime import datetime

# Add root to sys.path
sys.path.append(os.getcwd())

from config.settings import config
from engine.exchange_interface import ExchangeInterface

def verify_actions():
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print(f"\n--- 1. Checking DB Trade History (Last 10) ---")
    cursor.execute("""
        SELECT * FROM trade_history 
        ORDER BY timestamp DESC LIMIT 10
    """)
    rows = cursor.fetchall()
    if rows:
        for row in rows:
            ts = datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] Action: {row['action']} | Bot: {row['bot_id']} | Pair: {row['symbol']} | OrderID: {row['order_id']} | Notes: {row['notes']}")
    else:
        print("No history found.")

    print(f"\n--- 2. Checking Active Bots ---")
    cursor.execute("SELECT id, name, pair, direction FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    for b in bots:
        print(f"Bot {b['id']} ({b['name']}) {b['direction']} {b['pair']}")

    print("\n--- 3. Checking Exchange Closed Orders (BTC/USDC) ---")
    try:
        # Initialize Exchange
        # We need to force generic market type if needed, but default is likely fine
        interface = ExchangeInterface() 
        
        # Check specifically for BTC/USDC
        symbol = 'BTC/USDC'
        print(f"Fetching closed orders for {symbol}...")
        orders = interface.client.fetch_closed_orders(symbol, limit=20)
        
        # Sort by time desc
        orders.sort(key=lambda x: x['timestamp'], reverse=True)
        
        print(f"Found {len(orders)} closed orders.")
        for o in orders[:5]:
            ts = datetime.fromtimestamp(o['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] ID: {o['id']} | Side: {o['side']} | Type: {o['type']} | Amount: {o['amount']} | Price: {o['average']} | Status: {o['status']} | ClientID: {o.get('clientOrderId')}")
            
    except Exception as e:
        print(f"Exchange Check Failed: {e}")

    conn.close()

if __name__ == "__main__":
    verify_actions()
