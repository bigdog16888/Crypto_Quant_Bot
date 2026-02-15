
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future', validate=True)
market = ex.exchange.market("BTC/USDC")
precision = market.get('precision', {})
limits = market.get('limits', {})

print(f"Precision: {precision}")
print(f"Limits: {limits}")
print(f"Amount Precision: {precision.get('amount')}")
print(f"Amount Limit Min: {limits.get('amount', {}).get('min')}")
