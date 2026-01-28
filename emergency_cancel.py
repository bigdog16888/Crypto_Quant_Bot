
from engine.exchange_interface import ExchangeInterface

print("initializing exchange...")
ex = ExchangeInterface(market_type='future', validate=False)
pair = "BTC/USDC"

print(f"Cancelling all open orders for {pair}...")
try:
    ex.cancel_all_orders(pair)
    print("✅ All orders cancelled.")
except Exception as e:
    print(f"❌ Failed to cancel: {e}")

print("Checking open orders...")
orders = ex.fetch_open_orders(pair)
print(f"Open Orders Count: {len(orders)}")
for o in orders:
    print(f"  {o['id']} - {o['status']}")
