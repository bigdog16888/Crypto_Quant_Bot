
import sys
import os
import time
from datetime import datetime
import ccxt

sys.path.append(os.getcwd())
from config.settings import config
from engine.exchange_interface import ExchangeInterface

def forensic_search():
    print("--- FORENSIC TRADE SEARCH (00:10 - 00:25) ---")
    interface = ExchangeInterface()
    symbol = 'BTC/USDC'
    
    # Time Window: 2026-02-17 00:10 to 00:25
    # Timestamp now is approx 1771259100
    # Window start: 1771258200 (approx 15 mins ago)
    since_ts = int(time.time() * 1000) - (20 * 60 * 1000) 
    
    try:
        # Fetch My Trades (Executions)
        print(f"Fetching trades since {datetime.fromtimestamp(since_ts/1000)}")
        trades = interface.client.fetch_my_trades(symbol, since=since_ts, limit=50)
        
        print(f"Found {len(trades)} executed trades.")
        for t in trades:
            dt = datetime.fromtimestamp(t['timestamp']/1000).strftime('%H:%M:%S')
            cost = t['cost'] if 'cost' in t else (t['price'] * t['amount'])
            print(f"[{dt}] {t['side'].upper()} {t['amount']} @ {t['price']} | Role: {t['takerOrMaker']} | OrdID: {t['order']} | Fee: {t['fee']}")
            
    except Exception as e:
        print(f"Error fetching trades: {e}")

if __name__ == "__main__":
    forensic_search()
