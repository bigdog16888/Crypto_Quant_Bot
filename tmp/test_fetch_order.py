import asyncio
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def test_fetch_order():
    ex = ExchangeInterface('future')
    
    # Grid order OID
    oid = '66706454'
    print(f"Fetching order {oid} for SUIUSDC")
    
    try:
        order = ex.fetch_order(oid, 'SUIUSDC')
        print(f"Result: {order}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == '__main__':
    test_fetch_order()
