
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
    print("--- FORENSIC TRADE SEARCH v3 ---")
    try:
        interface = ExchangeInterface()
        symbol = 'BTC/USDC'
        
        # Look back 45 mins
        since_ts = int(time.time() * 1000) - (45 * 60 * 1000) 
        print(f"Fetching trades since {datetime.fromtimestamp(since_ts/1000)}")
        
        # Use .exchange (CCXT instance)
        # Check if fetch_my_trades is supported, if not, iterate closed orders
        if interface.exchange.has['fetchMyTrades']:
            print("Fetching executed trades...")
            trades = interface.exchange.fetch_my_trades(symbol, since=since_ts, limit=50)
        else:
            print("fetchMyTrades not supported, using fetchClosedOrders...")
            trades = interface.exchange.fetch_closed_orders(symbol, since=since_ts, limit=50)

        if not trades:
            print("No trades found.")
            return

        print(f"Found {len(trades)} records.")
        for t in trades:
            ts = t['timestamp'] / 1000
            dt = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            side = t['side'].upper()
            amt = t['amount']
            price = t.get('price', t.get('average', 0))
            order_id = t.get('order', t.get('id', 'N/A'))
            
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
