import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
from config.settings import config

ex = ExchangeInterface(market_type=config.MARKET_TYPE)
for oid in ['48550289', '48549641']:
    try:
        o = ex.exchange.fetch_order(oid, 'XAU/USDT:USDT')
        print(f"Order {oid}: status={o.get('status')} filled={o.get('filled')} amount={o.get('amount')}")
    except Exception as e:
        print(f"Order {oid}: ERROR {e}")
