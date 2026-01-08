import os
import sys
from dotenv import load_dotenv
import ccxt

# Load env
load_dotenv(override=True)

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
IS_TESTNET = os.getenv("TESTNET", "False").lower() == "true"

print(f"--- Debug Connection ---")
print(f"TESTNET MODE: {IS_TESTNET}")
if API_KEY:
    print(f"API Key Length: {len(API_KEY)} (First 4: {API_KEY[:4]}...)")
else:
    print("API Key: MISSING")

if API_SECRET:
    print(f"API Secret Length: {len(API_SECRET)}")
else:
    print("API Secret: MISSING")

if not API_KEY or not API_SECRET:
    sys.exit(1)

def test_connection(name, options):
    print(f"\n--- Testing {name} ---")
    try:
        exchange = ccxt.binance({
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'options': options,
            'enableRateLimit': True,
            'timeout': 5000
        })
        
        if IS_TESTNET:
            # Special handling: Skip set_sandbox_mode for Futures to avoid "not supported" error
            is_future = 'future' in options.get('defaultType', '') or 'delivery' in options.get('defaultType', '')
            
            if is_future:
                testnet_base = 'https://testnet.binancefuture.com'
                exchange.urls['api'].update({
                    'fapiPublic': f'{testnet_base}/fapi/v1',
                    'fapiPublicV2': f'{testnet_base}/fapi/v2',
                    'fapiPrivate': f'{testnet_base}/fapi/v1',
                    'fapiPrivateV2': f'{testnet_base}/fapi/v2',
                    'dapiPublic': f'{testnet_base}/dapi/v1',
                    'dapiPrivate': f'{testnet_base}/dapi/v1',
                })
            else:
                exchange.set_sandbox_mode(True)
            
        # For Testnet, we need to override URLs usually, or use set_sandbox_mode
        if 'sandbox' in name.lower():
            exchange.set_sandbox_mode(True)

        print(f"1. Fetching Time (Public)...")
        exchange.fetch_time()
        print("   [OK] Public API OK")

        print(f"2. Fetching Balance (Private)...")
        bal = exchange.fetch_balance()
        print("   [OK] Auth OK!")
        return True
    except Exception as e:
        print(f"   [FAIL] Failed: {e}")
        return False

# 1. Test Spot
test_connection("Spot (Mainnet)", {'defaultType': 'spot'})

# 2. Test Futures (USDT-M)
test_connection("Futures (USDT-M Mainnet)", {'defaultType': 'future'})

# 3. Test Futures (Coin-M)
test_connection("Futures (Coin-M Mainnet)", {'defaultType': 'delivery'})

