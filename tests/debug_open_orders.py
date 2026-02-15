
import sys
import os

# Robustly find project root
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

print(f"Project Root: {project_root}")
print(f"Sys Path: {sys.path[:3]}")

from engine.exchange_interface import ExchangeInterface
from engine.config import global_config

def debug_orders():
    print("Fetching open orders...")
    ex = ExchangeInterface(global_config.MARKET_TYPE)
    
    # Get active pairs from DB to be safe, or just check known ones
    pair = "BTC/USDC" 
    print(f"Checking {pair}...")
    
    orders = ex.fetch_open_orders(pair)
    print(f"Found {len(orders)} orders.")
    
    for o in orders:
        cid = o.get('clientOrderId', 'N/A')
        print(f"- ID: {o['id']} | CID: {cid} | Type: {o['type']} | Side: {o['side']} | Price: {o['price']}")

if __name__ == "__main__":
    debug_orders()
