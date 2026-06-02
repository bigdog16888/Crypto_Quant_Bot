import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def main():
    ex = ExchangeInterface()
    
    # 1. Fetch positions
    print("=== Exchange Positions ===")
    positions = ex.fetch_positions()
    sui_pos = [p for p in positions if 'SUI' in p.get('symbol', '')]
    if sui_pos:
        for p in sui_pos:
            print(f"symbol={p.get('symbol')} side={p.get('side')} contracts={p.get('contracts')} entryPrice={p.get('entryPrice')} size={p.get('size')}")
    else:
        print("No SUI positions found")
        
    # 2. Fetch open orders for SUI
    print("\n=== Exchange Open Orders ===")
    try:
        # Check standard symbol mapping for SUI. The DB has 'SUI/USDC:USDC' or SUI/USDT:USDT. Let's list all symbols.
        open_orders = ex.fetch_open_orders(None) # fetches all open orders
        sui_orders = [o for o in open_orders if 'SUI' in o.get('symbol', '')]
        if sui_orders:
            for o in sui_orders:
                print(f"id={o.get('id')} clientOrderId={o.get('clientOrderId')} symbol={o.get('symbol')} side={o.get('side')} type={o.get('type')} price={o.get('price')} amount={o.get('amount')} status={o.get('status')}")
        else:
            print("No SUI open orders found on exchange")
    except Exception as e:
        print(f"Error fetching open orders: {e}")

if __name__ == '__main__':
    main()
