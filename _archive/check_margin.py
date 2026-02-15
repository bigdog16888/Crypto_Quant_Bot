"""Check actual position and margin situation"""
from engine.exchange_interface import ExchangeInterface

def main():
    print("=== MARGIN & POSITION CHECK ===\n")
    
    # BTC/USDC (swap)
    ex = ExchangeInterface(market_type='swap')
    
    print("--- BALANCE ---")
    balance = ex.fetch_balance()
    for key in ['USDC', 'USDT', 'BTC']:
        if key in balance:
            b = balance[key]
            print(f"  {key}: Free={b.get('free', 0):.4f} | Used={b.get('used', 0):.4f} | Total={b.get('total', 0):.4f}")
    
    print("\n--- POSITIONS ---")
    try:
        positions = ex.fetch_positions()
        active = [p for p in positions if abs(float(p.get('contracts', 0) or 0)) > 0]
        
        if not active:
            print("  No open positions!")
        
        for p in active:
            sym = p.get('symbol', 'Unknown')
            side = p.get('side', 'Unknown')
            contracts = p.get('contracts', 0)
            entry = p.get('entryPrice', 0)
            notional = p.get('notional', 0)
            margin = p.get('initialMargin', 0)
            leverage = p.get('leverage', 'N/A')
            unrealized = p.get('unrealizedPnl', 0)
            
            print(f"  {sym}:")
            print(f"    Side: {side} | Contracts: {contracts}")
            print(f"    Entry: ${entry} | Notional: ${notional}")
            print(f"    Margin: ${margin} | Leverage: {leverage}x")
            print(f"    Unrealized PnL: ${unrealized}")
            
            # Raw info
            info = p.get('info', {})
            print(f"    [Raw] positionAmt: {info.get('positionAmt')}")
            print(f"    [Raw] positionInitialMargin: {info.get('positionInitialMargin')}")
            print(f"    [Raw] maintMargin: {info.get('maintMargin')}")
            
    except Exception as e:
        print(f"  Error: {e}")
    
    print("\n--- OPEN ORDERS ---")
    try:
        orders = ex.fetch_open_orders('BTC/USDC')
        print(f"  Count: {len(orders)}")
        for o in orders:
            print(f"  {o.get('id')} | {o.get('side')} | {o.get('amount')} @ ${o.get('price')} | {o.get('clientOrderId', 'N/A')}")
    except Exception as e:
        print(f"  Error: {e}")

if __name__ == "__main__":
    main()
