"""
Order Size Calculator - Shows real minimum order sizes for each pair.

Usage:
  python tools/order_info.py BTC/USDC
  python tools/order_info.py              # Shows all common pairs
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface

def show_order_info(symbol: str):
    ex = ExchangeInterface(market_type='future')
    info = ex.get_order_info(symbol)
    
    print(f"\n{'='*50}")
    print(f"  {symbol} Order Info")
    print(f"{'='*50}")
    print(f"  Current Price:    ${info['current_price']:,.2f}")
    print(f"  Step Size:        {info['step_size']} {symbol.split('/')[0]}")
    print(f"  Step Value:       ${info['step_value_usd']:,.2f} per step")
    print(f"  Min Qty:          {info['min_qty']} {symbol.split('/')[0]}")
    print(f"  Min Notional:     ${info['min_notional']}")
    print(f"  TRUE Minimum:     ${info['min_order_usd']:,.2f}")
    print()
    print("  Valid Order Sizes (first 5):")
    for i, size in enumerate(info['valid_sizes'], 1):
        print(f"    {i}. {size['qty']:.6f} = ${size['usd']:,.2f}")
    print()

def main():
    if len(sys.argv) > 1:
        symbol = sys.argv[1].upper()
        if '/' not in symbol:
            symbol = f"{symbol}/USDC"
        show_order_info(symbol)
    else:
        # Show common pairs
        pairs = ['BTC/USDC', 'ETH/USDC', 'BNB/USDC', 'SOL/USDC', 'ADA/USDT']
        print("\nOrder Size Information for Common Pairs")
        print("(Showing TRUE minimum after stepSize rounding)\n")
        for pair in pairs:
            try:
                show_order_info(pair)
            except Exception as e:
                print(f"  {pair}: Error - {e}")

if __name__ == "__main__":
    main()
