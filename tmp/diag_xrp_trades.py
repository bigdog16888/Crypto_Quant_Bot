import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    ex = ExchangeInterface('future')
    since = int((time.time() - 86400 * 3) * 1000)
    trades = ex.fetch_my_trades('XRPUSDC', since=since, limit=500)
    
    print("=== XRP Recent Binance Trades ===")
    for t in trades[-15:]:  # last 15 trades
        qty = float(t.get('amount', 0))
        side = t.get('side', '').lower()
        oid = str(t.get('order', ''))
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.get('timestamp', 0)/1000))
        print(f"[{dt}] OID {oid} ({side.upper()} {qty:.4f}): Price {t.get('price')}")
        
if __name__ == '__main__':
    check()
