
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

def signed_request(endpoint, method='GET', params=None):
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
    
    url = f"{BASE_URL}{endpoint}"
    if method == 'GET':
        url += f"?{query_string}&signature={signature}"
        print(f"GET {url}")
        res = requests.get(url, headers=headers)
    else:
        body = f"{query_string}&signature={signature}"
        print(f"POST {url} body={body}")
        res = requests.post(url, headers=headers, data=body)
        
    print(f"Status: {res.status_code}")
    print(f"Response: {res.text[:500]}")
    return res.json()

def check_keys():
    print("--- 1. Checking Account Balance (GET) ---")
    signed_request("/fapi/v2/balance")

    print("\n--- 2. Checking Position Mode (GET) ---")
    # Endpoint to get position mode
    res = signed_request("/fapi/v1/positionSide/dual")
    # {"dualSidePosition": true} means Hedge Mode
    
    print("\n--- 3. Checking Open Orders (GET) ---")
    signed_request("/fapi/v1/openOrders")

if __name__ == "__main__":
    check_keys()
