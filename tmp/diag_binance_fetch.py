import os, sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface('future')
try:
    order = ex.exchange.fetch_order('84663310', 'XRPUSDC')
    print("Fetch succeeded!")
    print(f"Status: {order.get('status')}, Filled: {order.get('filled')}")
except Exception as e:
    print(f"Fetch failed: {e}")
