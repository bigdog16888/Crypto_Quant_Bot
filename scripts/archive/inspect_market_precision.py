
import sys
import os
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def inspect_precision():
    print("--- Inspecting Market Precision (Demo FAPI) ---")
    
    # Initialize Interface
    interface = ExchangeInterface()
    
    # Ensure raw markets loaded
    interface._ensure_markets()
    
    target_pair = 'BTC/USDC'
    normalized = 'BTCUSDC'
    
    markets = interface.exchange.markets
    
    if target_pair in markets:
        print(f"FOUND {target_pair} in markets!")
        market = markets[target_pair]
    elif normalized in markets:
        print(f"FOUND {normalized} in markets!")
        market = markets[normalized]
    else:
        print(f"❌ {target_pair} NOT FOUND. Available keys sample: {list(markets.keys())[:5]}")
        return

    print("\n[Raw Precision Object]")
    print(json.dumps(market.get('precision'), indent=2, default=str))
    
    print("\n[Raw Limits Object]")
    print(json.dumps(market.get('limits'), indent=2, default=str))
    
    print("\n[Calculated Precision]")
    prec = interface.get_symbol_precision(target_pair)
    print(json.dumps(prec, indent=2))

if __name__ == "__main__":
    inspect_precision()
