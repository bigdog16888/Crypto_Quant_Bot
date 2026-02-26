from engine.exchange_interface import ExchangeInterface
import json
import time
from datetime import datetime

try:
    ex = ExchangeInterface('future')
    
    # 2 hours ago = ~7200000 ms
    since = int((time.time() - 7200) * 1000)
    
    # Needs to handle both USDT and USDC
    trades_usdc = ex.exchange.fetch_my_trades('BTC/USDC:USDC', since=since)
    
    print('=== BINANCE RAW TRADES (BTC/USDC) ===')
    for t in trades_usdc:
        print(f"[{t['datetime']}] {t['side']} {t['amount']} @ {t['price']} (ID: {t['id']})")
        
except Exception as e:
    print(f"Error: {e}")
