
import sys
import os
import time
from datetime import datetime
import ccxt

sys.path.append(os.getcwd())
try:
    from config.settings import config
    from engine.exchange_interface import ExchangeInterface
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def forensic_search():
    print("--- FORENSIC TRADE SEARCH ---")
    try:
        interface = ExchangeInterface()
        symbol = 'BTC/USDC'
        
        # Look back 30 mins
        since_ts = int(time.time() * 1000) - (30 * 60 * 1000) 
        print(f"Fetching trades since {datetime.fromtimestamp(since_ts/1000)}")
        
        # Direct CCXT call
        trades = interface.client.fetch_my_trades(symbol, since=since_ts, limit=50)
        
        if not trades:
            print("No trades found.")
            return

        print(f"Found {len(trades)} executed trades.")
        for t in trades:
            ts = t['timestamp'] / 1000
            dt = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            side = t['side'].upper()
            amt = t['amount']
            price = t['price']
            order_id = t['order']
            
            # Additional info
            info = t.get('info', {})
            client_oid = info.get('clientOrderId', 'N/A')
            
            print(f"[{dt}] {side} {amt} @ {price} | OrderID: {order_id} | ClientID: {client_oid}")
            
    except Exception as e:
        print(f"Error fetching trades: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    forensic_search()
