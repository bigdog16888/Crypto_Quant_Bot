import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
positions = [p for p in ex.fetch_positions() if abs(float(p.get('contracts') or p.get('size') or 0)) > 0]
print('=== REAL LIVE EXCHANGE POSITIONS ===')
for p in positions:
    print(f"{p.get('symbol'):<16} contracts: {p.get('contracts'):<8} side: {p.get('side'):<6} entry: {p.get('entryPrice')}")
print(f'\nTotal nonzero positions: {len(positions)}')
