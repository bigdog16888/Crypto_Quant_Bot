
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Test")

ex = ExchangeInterface(market_type='future')
symbol = 'BTC/USDC'
side = 'sell' # Short entry
amount_usd = 150 # Bot's base size approx
price = ex.get_last_price(symbol)
amount = amount_usd / price

print(f"Attempting to place {side} entry for {symbol} at {price} (Amount: {amount})")

params = {
    'clientOrderId': 'CQB_TEST_ENTRY',
    'postOnly': True
}

try:
    res = ex.create_order(symbol, 'limit', side, amount, price, params=params)
    print(f"Success! Order: {res['id']}")
except Exception as e:
    print(f"Failed: {e}")
