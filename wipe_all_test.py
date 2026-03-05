from config.settings import config
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

print("TESTNET config:", config.TESTNET)
print("DEMO_TRADING config:", config.DEMO_TRADING)
print("API_KEY begins with:", config.API_KEY[:5] if config.API_KEY else "None")

ex = ExchangeInterface('future')

try:
    positions = ex.fetch_all_positions()
    print("\nCurrent Positions to Flatten:")
    for p in positions:
        print(f"  {p.symbol} {p.side} Qty: {p.size} (Notional: {abs(p.size) * p.entry_price})")
except Exception as e:
    print("Error fetching positions:", e)
