import os
import sys
import json

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def deep_diag():
    ex = ExchangeInterface(market_type='future')
    print("--- DEEP EXCHANGE DIAGNOSTIC ---")
    
    # 1. Check Hedge Mode Sync
    try:
        mode = ex.exchange.fapiPrivateGetPositionSideDual()
        print(f"Hedge Mode (Dual Side): {json.dumps(mode, indent=2)}")
    except Exception as e:
        print(f"Failed to fetch Hedge Mode setting: {e}")

    # 2. Check Raw Positions for failing symbols
    symbols = ['BTC/USDC:USDC', 'XAU/USDT:USDT']
    try:
        positions = ex.exchange.fapiPrivateGetPositionRisk()
        print(f"\n--- POSITION RISK ({len(positions)} positions) ---")
        for pos in positions:
            if pos.get('symbol') in ['BTCUSDC', 'XAUUSDT']:
                print(json.dumps(pos, indent=2))
    except Exception as e:
        print(f"Failed to fetch position risk: {e}")

    # 3. Check Account Info
    try:
        account = ex.exchange.fapiPrivateGetAccount()
        print(f"\n--- ACCOUNT INFO ---")
        print(f"Account Multi-Assets Mode: {account.get('multiAssetsMargin')}")
    except Exception as e:
        print(f"Failed to fetch account info: {e}")

if __name__ == "__main__":
    deep_diag()
