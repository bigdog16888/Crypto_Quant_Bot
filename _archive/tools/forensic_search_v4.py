
import sys
import os
import time
from datetime import datetime
import secrets

sys.path.append(os.getcwd())
try:
    from config.settings import config
    from engine.exchange_interface import ExchangeInterface
except ImportError as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def forensic_search_raw():
    print("--- FORENSIC TRADE SEARCH v4 (RAW) ---")
    try:
        interface = ExchangeInterface()
        symbol = 'BTC/USDC' # Note: Raw API might expect BTCUSDC
        
        # Look back 60 mins
        since_ts = int(time.time() * 1000) - (60 * 60 * 1000) 
        print(f"Fetching trades since {datetime.fromtimestamp(since_ts/1000)}")
        
        # Attempt 'BTCUSDC'
        normalized_symbol = 'BTCUSDC'
        
        # Endpoint: /fapi/v1/userTrades
        endpoint = '/fapi/v1/userTrades'
        params = {
            'symbol': normalized_symbol,
            'startTime': since_ts,
            'limit': 50
        }
        
        print(f"Querying {endpoint} for {normalized_symbol}...")
        trades = interface._raw_request(endpoint, params=params)
        
        if not trades:
            print(f"No trades found for {normalized_symbol}. Trying BTCUSDT just in case...")
            # Fallback check
            params['symbol'] = 'BTCUSDT'
            trades = interface._raw_request(endpoint, params=params)
            
        if not trades:
            print("No trades found via Raw API.")
            return

        print(f"Found {len(trades)} records.")
        # Sort desc
        trades.sort(key=lambda x: x['time'], reverse=True)
        
        for t in trades:
            ts = t['time'] / 1000
            dt = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            side = t['side'].upper()
            amt = t['qty']
            price = t['price']
            order_id = t['orderId']
            # realizedPnl often included
            pnl = t.get('realizedPnl', '0')
            
            print(f"[{dt}] {side} {amt} @ {price} | OrderID: {order_id} | PnL: {pnl}")
            
    except Exception as e:
        print(f"Error fetching raw trades: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    forensic_search_raw()
