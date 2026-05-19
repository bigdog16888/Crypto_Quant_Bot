
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

def signed_request(endpoint, method='POST', params=None):
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
    # POST typically requires body
    body = f"{query_string}&signature={signature}"
    print(f"POST {url} body={body}")
    res = requests.post(url, headers=headers, data=body)
        
    print(f"Status: {res.status_code}")
    print(f"Response: {res.text}")
    return res.json()

def enable_hedge_mode():
    print("--- Enabling Hedge Mode (dualSidePosition=true) ---")
    # true for Hedge Mode, false for One-Way
    signed_request("/fapi/v1/positionSide/dual", method='POST', params={'dualSidePosition': 'true'})

    print("\n--- Verifying Mode ---")
    # We need a GET request helper here, duplicating for simplicity or reusing if I imported
    # reusing logic roughly:
    ts = int(time.time() * 1000)
    qs = f"timestamp={ts}"
    sig = hmac.new(API_SECRET.encode('utf-8'), qs.encode('utf-8'), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}/fapi/v1/positionSide/dual?{qs}&signature={sig}"
    res = requests.get(url, headers={"X-MBX-APIKEY": API_KEY})
    print(f"GET Mode: {res.text}")

if __name__ == "__main__":
    enable_hedge_mode()
