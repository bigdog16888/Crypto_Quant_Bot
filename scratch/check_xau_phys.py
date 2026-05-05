import os
import sys
import ccxt

# Add workspace to path
sys.path.append(os.getcwd())

try:
    from config.settings import config
    ex = ccxt.binanceusdm({
        'apiKey': config.API_KEY,
        'secret': config.API_SECRET,
        'options': {'defaultType': 'future'}
    })
    
    if config.DEMO_TRADING:
        ex.urls['api']['fapiPublic'] = 'https://demo-fapi.binance.com/fapi/v1'
        ex.urls['api']['fapi'] = 'https://demo-fapi.binance.com'

    positions = ex.fetch_positions(['XAU/USDT:USDT'])
    found = False
    for p in positions:
        amt = float(p.get('info', {}).get('positionAmt', 0))
        if amt != 0:
            print(f"SYMBOL: {p['symbol']} | QTY: {amt} | SIDE: {p['info'].get('positionSide', 'BOTH')}")
            found = True
    
    if not found:
        print("XAUUSDT Physical Position is 0.00")
        
except Exception as e:
    print(f"Error: {e}")
