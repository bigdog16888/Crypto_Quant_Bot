
import sys
import os
import json
import logging
from engine.exchange_interface import ExchangeInterface
from config.settings import config

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DebugOrder")

def check_order():
    symbol = 'BTC/USDC'
    order_id = '558272026'
    
    print(f"--- Checking Order {order_id} for {symbol} on {config.MARKET_TYPE} Testnet={config.TESTNET} ---")
    
    # Initialize Exchange
    try:
        ex = ExchangeInterface(market_type='future', validate=False) # Assuming Bot 41 was futures
        
        # 1. Try to fetch the specific order
        try:
            order = ex.fetch_order(order_id, symbol)
            print("\n✅ ORDER FOUND:")
            print(json.dumps(order, indent=2, default=str))
        except Exception as e:
            print(f"\n❌ Fetch Order Failed: {e}")
            
        # 2. Fetch Open Orders for the symbol
        try:
            print(f"\n--- Checking Open Orders for {symbol} ---")
            open_orders = ex.fetch_open_orders(symbol)
            if open_orders:
                for o in open_orders:
                    print(f"Open Order: {o['id']} - {o['side']} {o['amount']} @ {o['price']}")
            else:
                print("No open orders found.")
        except Exception as e:
            print(f"Fetch Open Orders Failed: {e}")

        # 3. Check Account Positions (to verify if we have ANY exposure)
        try:
            print(f"\n--- Checking Positions for {symbol} ---")
            positions = ex.fetch_positions()
            found = False
            for p in positions:
                if p['symbol'] == symbol or float(p['contracts']) > 0:
                    print(f"Position: {p['symbol']} Size: {p['contracts']} Entry: {p['entryPrice']}")
                    found = True
            if not found:
                print("No active positions found.")
                
        except Exception as e:
            print(f"Fetch Positions Failed: {e}")

    except Exception as e:
        print(f"CRITICAL: Failed to init exchange: {e}")

if __name__ == "__main__":
    check_order()
