
from engine.exchange_interface import ExchangeInterface

print("Initializing Exchange Interface...")
ex = ExchangeInterface(market_type='future', validate=True)

test_pairs = ['BTC/USDC', 'ETH/USDC', 'XRP/USDC']

print("\n--- Testing Safe Min Size Calculation ---")
for pair in test_pairs:
    print(f"\nChecking {pair}...")
    try:
        ticker = ex.fetch_ticker(pair)
        price = ticker['last']
        print(f"  Price: ${price}")
        
        safe_min = ex.calculate_safe_min_size(pair, price)
        print(f"  ✅ Safe Min Size: ${safe_min:.2f}")
        
        # Verify why
        market = ex.exchange.market(pair)
        min_cost = market['limits']['cost']['min']
        step_size = market['precision']['amount']
        
        print(f"  Details: Min Cost ${min_cost}, Step {step_size}")
        
        # Check standard calc
        standard_qty = min_cost / price
        rounded_down = int(standard_qty / step_size) * step_size
        rounded_down_val = rounded_down * price
        
        print(f"  Standard (Round Down): ${rounded_down_val:.2f} (Valid: {rounded_down_val >= min_cost})")
        
        if safe_min > min_cost * 1.5:
             print(f"  ⚠️  Notice: Safe Min is significantly higher than Min Cost due to Step Size constraint.")
             
    except Exception as e:
        print(f"  ❌ Error: {e}")
