import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
from config.settings import config
import time

ex = ExchangeInterface(market_type='future') # specify exactly
try:
    # use fetch_my_trades
    trades = ex.exchange.fetch_my_trades('XAU/USDT:USDT', limit=20)
    for t in trades[-10:]:
        print(f"Trade {t.get('id')} - Order: {t.get('order')} - Side: {t.get('side')} - Amount: {t.get('amount')} - Price: {t.get('price')} - TS: {t.get('timestamp')}")
except Exception as e:
    print(f"Error fetching trades: {e}")
