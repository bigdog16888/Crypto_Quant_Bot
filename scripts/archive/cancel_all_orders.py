
import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
BASE_URL = "https://demo-fapi.binance.com"

def signed_request(endpoint, method='DELETE', params=None):
    if not params: params = {}
    params['timestamp'] = int(time.time() * 1000)
    query_string = urlencode(params)
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "X-MBX-APIKEY": API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    print(f"{method} {url}")
    res = requests.request(method, url, headers=headers)
        
    print(f"Status: {res.status_code}")
    print(f"Response: {res.text}")
    return res.json()

def cancel_all():
    print("--- Canceling All Orders (BTCUSDC) ---")
    signed_request("/fapi/v1/allOpenOrders", method='DELETE', params={'symbol': 'BTCUSDC'})

if __name__ == "__main__":
    cancel_all()
