import os
import time
import hmac
import hashlib
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('BINANCE_TESTNET_API_KEY', '').strip()
API_SECRET = os.getenv('BINANCE_TESTNET_API_SECRET', '').strip()
BASE_URL = 'https://testnet.binancefuture.com'

print(f"Key loaded: {API_KEY[:5]}... len={len(API_KEY)}")

def create_order(symbol, side, qty):
    endpoint = '/fapi/v1/order'
    timestamp = int(time.time() * 1000)
    
    params = {
        'symbol': symbol,
        'side': side.upper(),
        'type': 'MARKET',
        'quantity': qty,
        'reduceOnly': 'true',
        'timestamp': timestamp,
    }
    
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {
        'X-MBX-APIKEY': API_KEY
    }
    
    response = requests.post(url, headers=headers)
    return response.json()

sol_reduce = 1.35
eth_reduce = 0.207

print("Flattening specific physical offsets via direct REST API...")
try:
    print(f"Selling (reducing shorts) - Market Buy:")
    print(f"SOL: {sol_reduce} contracts")
    res_sol = create_order('SOLUSDC', 'BUY', sol_reduce)
    print("SOL Response:", res_sol)
except Exception as e:
    print("SOL Error:", e)

try:
    print(f"ETH: {eth_reduce} contracts")
    res_eth = create_order('ETHUSDC', 'BUY', eth_reduce)
    print("ETH Response:", res_eth)
except Exception as e:
    print("ETH Error:", e)

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("DELETE FROM trades")
c.execute("UPDATE bot_orders SET status='reset_cleared' WHERE status IN ('open', 'filled', 'missing', 'closed')")
c.execute("UPDATE bots SET status='Scanning'")
conn.commit()
conn.close()
print("Database cleanly wiped and reset to Scanning.")
