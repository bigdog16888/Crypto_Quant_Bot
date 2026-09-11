#!/usr/bin/env python
"""Live exchange GET for BTC position + open orders (10016/100317). Read-only."""
import json, os, sys
sys.path.insert(0, r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')
from dotenv import load_dotenv
load_dotenv(dotenv_path=r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\.env')
from engine.binance_client import get_binance_client
client = get_binance_client()

print('=== POSITIONS ===')
positions = client.futures_account_positions()
btc = [p for p in positions if p.get('symbol') == 'BTCUSDC']
for p in btc:
    print(json.dumps({k: p[k] for k in ['symbol','positionAmt','notional','leverage','entryPrice','markPrice','unRealizedProfit','positionSide']}, indent=2))

print('=== OPEN ORDERS ===')
orders = client.futures_get_open_orders(symbol='BTCUSDC')
for o in orders:
    print(json.dumps({k: o[k] for k in ['orderId','clientOrderId','side','type','status','origQty','price','stopPrice','updateTime']}, indent=2))
