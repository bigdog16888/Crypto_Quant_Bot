
import os
import sys
import ccxt
from dotenv import load_dotenv

# Load .env
load_dotenv()

def check():
    api_key = os.getenv('BINANCE_API_KEY')
    secret = os.getenv('BINANCE_SECRET') or os.getenv('BINANCE_API_SECRET')
    
    if not api_key or not secret:
        print(f"❌ CREDENTIALS MISSING: Key={bool(api_key)}, Secret={bool(secret)}")
        return

    ex = ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'options': {'defaultType': 'future'}
    })
    
    ex.set_sandbox_mode(True)
    
    try:
        # Fetch JUST the position for BTC/USDC
        print("🔍 Fetching Position for BTC/USDC...")
        positions = ex.fetch_positions(['BTC/USDC'])
        for p in positions:
            if p['symbol'] == 'BTC/USDC':
                print(f"✅ POSITION: {p['side']} {p['contracts']} ({p['notional']} USD) @ {p['entryPrice']}")
                print(f"   Raw: {p}")
                return
        print("❌ No Position Found")
    except Exception as e:
        print(f"⚠️ Error: {e}")

if __name__ == "__main__":
    check()
