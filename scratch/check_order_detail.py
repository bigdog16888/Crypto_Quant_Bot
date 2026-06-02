import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def run():
    ex = ExchangeInterface()
    pair = 'ETH/USDC:USDC'
    cid = 'CQB_100316_TP_78_BE_FB'
    
    print("--- FETCH OPEN ORDERS ---")
    open_orders = ex.fetch_open_orders(pair)
    for o in open_orders:
        if o.get('clientOrderId') == cid:
            print("Found in open_orders:", o)
            
    print("\n--- FETCH CLOSED ORDERS ---")
    closed_orders = ex.fetch_closed_orders(pair, limit=50)
    for o in closed_orders:
        if o.get('clientOrderId') == cid:
            print("Found in closed_orders:", o)

if __name__ == '__main__':
    run()
