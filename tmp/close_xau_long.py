import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

def close_xau_long():
    ex = ExchangeInterface('future')
    
    # Fetch current physical position to confirm size
    positions = ex.fetch_positions()
    xau_pos = None
    for p in (positions or []):
        if 'XAU' in str(p.get('symbol', '')):
            xau_pos = p
            break
    
    if not xau_pos:
        print("No XAU position found on exchange. Already flat.")
        return
    
    contracts = float(xau_pos.get('contracts', 0))
    side = xau_pos.get('side', '')
    
    print(f"XAU position: {contracts} oz ({side}) @ entry {xau_pos.get('entryPrice')}")
    
    if side.lower() != 'long' or contracts <= 0:
        print(f"Position is not LONG or is zero. No action needed.")
        return
    
    # Sell to close the LONG (reduceOnly)
    print(f"Placing MARKET SELL {contracts} XAUUSDT to close LONG...")
    
    try:
        result = ex.create_order(
            symbol='XAUUSDT',
            order_type='MARKET',
            side='SELL',
            amount=contracts,
            params={'reduceOnly': True}
        )
        print(f"Order placed: {result}")
    except Exception as e:
        print(f"Error placing order: {e}")

if __name__ == '__main__':
    close_xau_long()
