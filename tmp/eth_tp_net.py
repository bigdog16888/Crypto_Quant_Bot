import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def check():
    ex = ExchangeInterface('future')
    since = int((time.time() - 86400 * 2) * 1000)
    trades = ex.fetch_my_trades('ETHUSDC', since=since, limit=500)
    
    # We want to calculate the running net position leading up to OID 271069793 and 271126823
    # 271069793 is the BUY 5.6730 (TP of cycle 3)
    # 271126823 is the SELL 0.0100 (Entry of cycle 4)
    
    net = 0.0
    print("=== Reconstructing Binance Physical Net ===")
    for t in trades:
        qty = float(t.get('amount', 0))
        side = t.get('side', '').lower()
        oid = str(t.get('order', ''))
        
        # For a SHORT bot perspective, sell = + to short pos, buy = - to short pos
        if side == 'sell':
            net += qty
        else:
            net -= qty
            
        if oid in ['271069793', '271126823']:
            dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.get('timestamp', 0)/1000))
            print(f"[{dt}] OID {oid} ({side.upper()} {qty:.4f}): Running Physical Net SHORT = {net:.4f}")
            
if __name__ == '__main__':
    check()
