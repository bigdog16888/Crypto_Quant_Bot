
import sys
import os
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface

def cancel_specific_order(order_id, symbol):
    print(f"--- CANCELLING ORDER {order_id} ({symbol}) ---")
    try:
        ex = ExchangeInterface(market_type='future')
        res = ex.cancel_order(order_id, symbol)
        if res:
            print(f"✅ Order {order_id} Cancelled Successfully.")
        else:
            print(f"⚠️ Failed to cancel order (might be already closed).")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    # Cancel the found ghost order
    cancel_specific_order('25613318', 'XAUUSDT')
