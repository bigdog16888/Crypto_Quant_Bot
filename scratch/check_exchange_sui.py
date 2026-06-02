import sys
import os
sys.path.insert(0, '.')

from engine.exchange_interface import ExchangeInterface

def check():
    ex = ExchangeInterface(market_type='future')
    print("Fetching ALL SUI/USDC trades from exchange since 08:00 today...")
    trades = ex.fetch_my_trades('SUI/USDC:USDC', since=int(1779840000 * 1000), limit=1000)
    print(f"Total SUI trades fetched: {len(trades)}")
    
    by_order = {}
    for t in trades:
        oid = t.get('order')
        amount = float(t.get('amount') or 0)
        by_order[oid] = by_order.get(oid, 0.0) + amount
        
    print("\nSummary of filled amounts by order on exchange:")
    for oid, filled_sum in sorted(by_order.items()):
        print(f"Order ID: {oid} | Total Filled: {filled_sum:.6f}")

if __name__ == '__main__':
    check()
