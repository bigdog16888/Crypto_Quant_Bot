
import requests
import json

def check_raw_exchange_info():
    url = "https://demo-fapi.binance.com/fapi/v1/exchangeInfo"
    print(f"Fetching {url}...")
    try:
        res = requests.get(url)
        data = res.json()
        
        target = "BTCUSDC"
        found = False
        
        for s in data['symbols']:
            if s['symbol'] == target:
                found = True
                print(f"\n--- SYMBOL: {target} ---")
                print(f"BaseAssetPrecision: {s['baseAssetPrecision']}")
                print(f"QuotePrecision: {s['quotePrecision']}")
                print(f"QuantityPrecision: {s['quantityPrecision']}")
                print(f"PricePrecision: {s['pricePrecision']}")
                
                print("\nFILTERS:")
                for f in s['filters']:
                    if f['filterType'] in ['LOT_SIZE', 'PRICE_FILTER', 'MARKET_LOT_SIZE']:
                        print(json.dumps(f, indent=2))
                break
                
        if not found:
            print(f"Symbol {target} not found!")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_raw_exchange_info()
