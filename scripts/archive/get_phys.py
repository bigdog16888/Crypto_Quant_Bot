from engine.exchange_interface import ExchangeInterface
import logging

logging.basicConfig(level=logging.INFO)
ex = ExchangeInterface()
phys = ex.fetch_positions()
for p in phys:
    if 'BTC' in p['symbol']:
        print(p)
