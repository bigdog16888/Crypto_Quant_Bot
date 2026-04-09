import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    # Check ETH trade history from Binance to understand the 0.001 gap
    ex = ExchangeInterface('future')
    
    since = int((time.time() - 86400 * 2) * 1000)
    trades = ex.fetch_my_trades('ETHUSDC', since=since, limit=500)
    
    print(f"Fetched {len(trades)} ETH trades")

    # Calculate net qty
    net_buy = 0.0
    net_sell = 0.0
    for t in trades:
        qty = float(t.get('amount', 0))
        side = str(t.get('side', '')).lower()
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        oid = t.get('order', '')
        print(f"  [{dt}] {side.upper()}: {qty:.4f} | OID: {oid}")
        if side == 'buy':
            net_buy += qty
        else:
            net_sell += qty
    
    print(f"\nNet for SHORT bot: bought (means short entry) {net_buy}, sold (means short exit/TP) {net_sell}")
    print(f"Net short position = {net_buy - net_sell:.6f} ETH")

if __name__ == '__main__':
    check()
