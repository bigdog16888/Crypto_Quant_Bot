"""Close ALL positions on exchange and cancel all orders for full reset"""
from engine.exchange_interface import ExchangeInterface
import sqlite3

def main():
    print("=== FULL EXCHANGE RESET ===\n")
    
    # 1. Close BTC/USDC positions
    print("--- Closing BTC/USDC ---")
    ex_swap = ExchangeInterface(market_type='swap')
    
    try:
        positions = ex_swap.fetch_positions()
        btc_pos = None
        for p in positions:
            if 'BTC' in p.get('symbol', '') and 'USDC' in p.get('symbol', ''):
                contracts = float(p.get('contracts', 0) or 0)
                if contracts != 0:
                    btc_pos = p
                    break
        
        if btc_pos:
            contracts = abs(float(btc_pos.get('contracts', 0)))
            side = btc_pos.get('side', '')
            close_side = 'sell' if side == 'long' else 'buy'
            
            print(f"  Found: {contracts} {side}")
            print(f"  Closing with {close_side} market order...")
            
            result = ex_swap.create_order(
                symbol='BTC/USDC',
                type='market',
                side=close_side,
                amount=contracts,
                params={'reduceOnly': True}
            )
            print(f"  ✅ Closed: {result.get('id')}")
        else:
            print("  No BTC/USDC position to close")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    # Cancel any BTC orders
    try:
        orders = ex_swap.fetch_open_orders('BTC/USDC')
        for o in orders:
            ex_swap.cancel_order(o['id'], 'BTC/USDC')
            print(f"  Cancelled order: {o['id']}")
    except Exception as e:
        print(f"  Order cancel error: {e}")
    
    # 2. Close XAU/USDT positions
    print("\n--- Closing XAU/USDT ---")
    ex_future = ExchangeInterface(market_type='future')
    
    try:
        positions = ex_future.fetch_positions()
        xau_pos = None
        for p in positions:
            if 'XAU' in p.get('symbol', ''):
                contracts = float(p.get('contracts', 0) or 0)
                if contracts != 0:
                    xau_pos = p
                    break
        
        if xau_pos:
            contracts = abs(float(xau_pos.get('contracts', 0)))
            side = xau_pos.get('side', '')
            close_side = 'sell' if side == 'long' else 'buy'
            
            print(f"  Found: {contracts} {side}")
            print(f"  Closing with {close_side} market order...")
            
            result = ex_future.create_order(
                symbol='XAU/USDT',
                type='market',
                side=close_side,
                amount=contracts,
                params={'reduceOnly': True}
            )
            print(f"  ✅ Closed: {result.get('id')}")
        else:
            print("  No XAU/USDT position to close")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    # Cancel any XAU orders
    try:
        orders = ex_future.fetch_open_orders('XAU/USDT')
        for o in orders:
            ex_future.cancel_order(o['id'], 'XAU/USDT')
            print(f"  Cancelled order: {o['id']}")
    except Exception as e:
        print(f"  Order cancel error: {e}")
    
    # 3. Reset DB
    print("\n--- Resetting Database ---")
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    c.execute("UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, target_tp_price=0, entry_confirmed=0")
    c.execute("UPDATE bot_orders SET status='closed' WHERE status='open'")
    conn.commit()
    conn.close()
    print("  ✅ DB reset complete")
    
    print("\n=== FULL RESET COMPLETE ===")

if __name__ == "__main__":
    main()
