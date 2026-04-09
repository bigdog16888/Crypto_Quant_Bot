import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    ex = ExchangeInterface('future')
    
    since = int((time.time() - 86400 * 3) * 1000)
    trades = ex.fetch_my_trades('XAUUSDT', since=since, limit=500)
    
    print(f"Fetched {len(trades)} XAU trades")
    
    net_qty = 0.0
    for t in trades:
        qty = float(t.get('amount', 0))
        side = str(t.get('side', '')).lower()
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        oid = t.get('order', '')
        
        # SHORT bot: buy = short close/TP, sell = short entry
        if side == 'buy':
            net_qty += qty   # closing short = reducing negative position
        else:
            net_qty -= qty   # opening short = adding to negative position
            
        print(f"  [{dt}] {side.upper()}: {qty:.4f} | OID: {oid} | running_net: {net_qty:.4f}")

    print(f"\nFINAL net (positive=LONG remaining, negative=SHORT remaining): {net_qty:.4f}")

if __name__ == '__main__':
    check()
