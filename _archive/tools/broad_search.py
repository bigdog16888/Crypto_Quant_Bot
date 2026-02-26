
import sqlite3
import os
import sys
import time
from datetime import datetime

sys.path.append(os.getcwd())
from config.settings import config
from engine.exchange_interface import ExchangeInterface

def find_any_order():
    print("\n--- Searching for ANY recent valid order (Since restart) ---")
    try:
        interface = ExchangeInterface()
        orders = interface.client.fetch_closed_orders('BTC/USDC', limit=50)
        
        # Filter for orders in the last 30 minutes
        # Current time is approx 00:25. Restart was 00:15.
        cutoff = time.time() - 1800 # 30 mins ago
        
        orders.sort(key=lambda x: x['timestamp'], reverse=True)
        
        for o in orders:
            ts = o['timestamp'] / 1000
            if ts < cutoff: break
            
            dt = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            print(f"[{dt}] {o['side'].upper()} {o['amount']} {o['symbol']} | Type: {o['type']} | Status: {o['status']} | ID: {o['id']} | ClientID: {o.get('clientOrderId')}")
            
    except Exception as e:
        print(f"Exchange error: {e}")

if __name__ == "__main__":
    find_any_order()
