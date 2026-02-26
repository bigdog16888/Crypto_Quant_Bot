import sys
import os
import json

sys.path.append(os.getcwd())
try:
    from engine.exchange_interface import ExchangeInterface
    from config.settings import config
except:
    print("Import failed.")

def cancel_orders_10000():
    print("--- CANCELLING ORDERS FOR BOT 10000 ---", flush=True)
    try:
        ex = ExchangeInterface(market_type='future')
        
        # Fetch Open Orders
        orders = ex.fetch_open_orders(symbol='BTC/USDC')
        if not orders:
            print("  No open orders found on BTC/USDC.")
            return

        bot_orders = [o for o in orders if o.get('clientOrderId', '').startswith('CQB_10000_')]
        print(f"  Found {len(bot_orders)} orders for Bot 10000.")
        
        for o in bot_orders:
            print(f"  Cancelling {o['id']} ({o['type']} {o['side']} {o['amount']})...")
            try:
                ex.cancel_order(o['id'], 'BTC/USDC')
                print("    ✅ Cancelled.")
            except Exception as ignore:
                print(f"    ❌ Failed: {ignore}")
                
        print("--- DONE ---")
            
    except Exception as e:
        print(f"CRASH: {e}")

if __name__ == "__main__":
    cancel_orders_10000()
