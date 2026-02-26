
import sys
import os
import json
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def list_all_orders():
    print("--- FETCHING ALL OPEN ORDERS ---")
    try:
        ex = ExchangeInterface(market_type='future')
        orders = ex.fetch_open_orders()
        
        if not orders:
            print("✅ No open orders found.")
        else:
            print(f"⚠️ FOUND {len(orders)} OPEN ORDERS:")
            for o in orders:
                print(f"  [ID: {o['id']}] {o['symbol']} {o['side'].upper()} {o['amount']} @ {o['price']} | ClientID: {o['clientOrderId']}")
                
    except Exception as e:
        print(f"❌ Failed to fetch orders: {e}")

if __name__ == "__main__":
    list_all_orders()
