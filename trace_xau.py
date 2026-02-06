"""Trace ALL XAU orders on exchange to find the SHORT source"""
from engine.exchange_interface import ExchangeInterface
import time

ex = ExchangeInterface(market_type='future')

# Get ALL order history for XAU from exchange - both fills and orders
print("Fetching all XAU trades from exchange...")

# Trades (fills)
since = int((time.time() - 30 * 24 * 3600) * 1000)  # 30 days
try:
    trades = ex.exchange.fetch_my_trades('XAU/USDT:USDT', since=since, limit=100)
    
    print(f"\nXAU TRADES (FILLS) - Last 30 days: {len(trades)}")
    print("-" * 100)
    
    buys = [t for t in trades if t.get('side') == 'buy']
    sells = [t for t in trades if t.get('side') == 'sell']
    
    print(f"Total BUYS: {len(buys)}")
    print(f"Total SELLS: {len(sells)}")
    
    print("\nLast 20 trades:")
    for t in trades[-20:]:
        cid = t.get('info', {}).get('clientOrderId', 'no-cid')
        print(f"  {t.get('datetime')} | {t.get('side'):4} | {t.get('amount')} @ ${t.get('price')} | CID: {cid[:30] if cid else 'none'}")
    
    # Sum up
    total_buy_qty = sum(float(t.get('amount', 0)) for t in buys)
    total_sell_qty = sum(float(t.get('amount', 0)) for t in sells)
    net_position = total_buy_qty - total_sell_qty
    
    print(f"\nNET POSITION CALCULATION:")
    print(f"  Total Bought: {total_buy_qty:.6f}")
    print(f"  Total Sold: {total_sell_qty:.6f}")
    print(f"  Net: {net_position:.6f} ({'LONG' if net_position > 0 else 'SHORT'})")
    
    # Current position check
    print("\nCURRENT EXCHANGE POSITION:")
    positions = ex.fetch_positions()
    for p in positions:
        if 'XAU' in p.get('symbol', ''):
            print(f"  {p.get('symbol')}: {p.get('side')} {p.get('contracts')}")
    
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
