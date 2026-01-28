"""
Check Exchange Limits and Market Data for BTC/USDC
"""
import sys
import time
from engine.exchange_interface import ExchangeInterface

print("Initializing Exchange Interface...")
try:
    ex = ExchangeInterface(market_type='future', validate=True)
except Exception as e:
    print(f"Failed to init exchange: {e}")
    sys.exit(1)

pair = "BTC/USDC"
print(f"\n--- Checking {pair} ---")

# 1. Ticker Data
print("\nFetching Ticker...")
ticker = ex.fetch_ticker(pair)
if ticker:
    print(f"  Last: {ticker.get('last')}")
    print(f"  Bid:  {ticker.get('bid')}")
    print(f"  Ask:  {ticker.get('ask')}")
    if not ticker.get('bid') or not ticker.get('ask'):
        print("  ❌ MISSING BID/ASK! This will cause entry to fail.")
else:
    print("  ❌ Failed to fetch ticker")

# 2. Min Order Size
print("\nChecking Limits...")
try:
    # Get current price for calculation
    price = ticker.get('last') if ticker else 90000
    
    # Get standard limits
    market = ex.exchange.market(pair)
    limits = market.get('limits', {})
    print(f"  Market Limits: {limits}")
    
    # Get specific min cost/amount
    min_cost = limits.get('cost', {}).get('min')
    min_amount = limits.get('amount', {}).get('min')
    
    print(f"  Min Cost (Notional): ${min_cost}")
    print(f"  Min Amount (Qty): {min_amount} BTC")
    
    # Calculate for $110
    base_size = 110.0
    qty = base_size / price
    notional = qty * price
    
    print(f"\nTest Configuration (Base Size: ${base_size}):")
    print(f"  Qty: {qty:.6f} BTC")
    print(f"  Notional: ${notional:.2f}")
    
    if min_cost and notional < min_cost:
        print(f"  ❌ FAIL: Notional ${notional:.2f} < Min Cost ${min_cost}")
    elif min_amount and qty < min_amount:
        print(f"  ❌ FAIL: Qty {qty:.6f} < Min Amount {min_amount}")
    else:
        print("  ✅ PASS: Size is visibly sufficient")

    # Use helper method
    print("\nVerifying with get_min_order_usd helper:")
    min_usd = ex.get_min_order_usd(pair, price)
    print(f"  Helper returned Min USD: ${min_usd}")
    
except Exception as e:
    print(f"  Error checking limits: {e}")

