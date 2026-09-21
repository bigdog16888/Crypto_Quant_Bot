import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
pos = ex.fetch_positions()

print('=== LIVE BINANCE POSITIONS (reference) ===')
for p in pos:
    c = float(p.get('contracts') or p.get('size') or 0)
    if abs(c) > 1e-12:
        print(f"{p.get('symbol'):<16} contracts: {c:<10.4f}  side: {p.get('side')}")
print()
print(f'Total nonzero: {sum(1 for p in pos if abs(float(p.get("contracts") or p.get("size") or 0)) > 1e-12)}')
