import requests
import time
import hmac
import hashlib
from config.settings import config
import json

def get_demo_positions():
    base_url = "https://demo-fapi.binance.com"
    endpoint = "/fapi/v2/positionRisk"
    
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}&recvWindow=60000"
    
    signature = hmac.new(
        config.API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "X-MBX-APIKEY": config.API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    res = requests.get(url, headers=headers)
    
    if res.status_code == 200:
        pos = [p for p in res.json() if float(p['positionAmt']) != 0]
        print(json.dumps([{'sym': p['symbol'], 'amt': p['positionAmt'], 'entry': p['entryPrice']} for p in pos], indent=2))
    else:
        print(f"Error: {res.text}")

if __name__ == '__main__':
    get_demo_positions()
