import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
trades = ex.fetch_my_trades('SUI/USDC:USDC', limit=5)

print('=== RECENT SUI TRADES ON BINANCE ===')
for t in trades:
    info = t.get('info', {})
    cid = info.get('clientOrderId') or t.get('clientOrderId')
    print(t.get('datetime'), t.get('side'), t.get('amount'), '@', t.get('price'), 'cid:', cid, 'orderId:', t.get('order'))
print(f'\nTotal recent trades returned: {len(trades)}')
