import requests
import json
import sys

try:
    r2 = requests.get('https://testnet.binancefuture.com/fapi/v1/exchangeInfo')
    for s in r2.json()['symbols']:
        if s['symbol'] == 'XAUUSDT':
            print(json.dumps(s, indent=2))
except Exception as e:
    print('Error:', e)
