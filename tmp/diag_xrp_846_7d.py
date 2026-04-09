import os, sys, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface('future')
since = int((time.time() - 86400 * 7) * 1000)
trades = ex.fetch_my_trades('XRPUSDC', since=since, limit=1000)
for t in trades:
    if str(t.get('order', '')) == '84638706':
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.get('timestamp', 0)/1000))
        print(f"[{dt}] Trade for 84638706: {t.get('amount')} | clientOrderId: {t.get('clientOrderId')}")
