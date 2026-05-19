from engine.exchange_interface import ExchangeInterface
import logging
logging.basicConfig(level=logging.WARNING)

ex = ExchangeInterface()
phys = ex.fetch_positions()
for p in phys:
    if 'LINK' in p['symbol']:
        print(f"LINK physical: contracts={p['contracts']}, side={p['side']}, entry={p['entryPrice']}, pnl={p['unrealizedPnl']}")
