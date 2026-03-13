import sys; sys.path.append('.')
from engine.exchange_interface import ExchangeInterface

try:
    ex = ExchangeInterface(market_type='future')
    hist = ex.fetch_my_trades('SUI/USDC:USDC', limit=100)
    
    total_short = 0
    total_long = 0
    
    print("Listing last 30 trades:")
    for g in hist[-30:]:
        amt = float(g.get('amount'))
        side = g.get('side', '')
        cid = g.get('info', {}).get('clientOrderId', '')
        print(f"{side.upper()} {amt} - CID: {cid}")
        
    for g in hist:
        amt = float(g.get('amount'))
        side = g.get('side', '')
        if side.lower() == 'sell': total_short += amt
        if side.lower() == 'buy': total_long += amt

    print(f"\nNet Physical Trades Sum (limit 100): Sell {total_short} vs Buy {total_long} = Net {-total_short + total_long}")

except Exception as e:
    print('Error:', e)
