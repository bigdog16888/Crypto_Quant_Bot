import requests
import os
import sys

def test_key_environment():
    # Load keys manually again to be strictly sure
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root_dir, '.env')
    
    api_key = None
    try:
        with open(env_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                if line.strip().upper().startswith('BINANCE_API_KEY='):
                    api_key = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    except:
        pass

    if not api_key:
        print("Could not load BINANCE_API_KEY")
        return

    print(f"Testing Key: {api_key[:10]}...")

    # 1. Test Demo Futures
    print("\n--- TEST 1: DEMO FUTURES (https://demo-fapi.binance.com) ---")
    try:
        resp = requests.post(
            'https://demo-fapi.binance.com/fapi/v1/listenKey',
            headers={'X-MBX-APIKEY': api_key}
        )
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")

    # 2. Test Mainnet Futures (Just in case user put real keys)
    print("\n--- TEST 2: MAINNET FUTURES (https://fapi.binance.com) ---")
    try:
        resp = requests.post(
            'https://fapi.binance.com/fapi/v1/listenKey',
            headers={'X-MBX-APIKEY': api_key}
        )
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_key_environment()
