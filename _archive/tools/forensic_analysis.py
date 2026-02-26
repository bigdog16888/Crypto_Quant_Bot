import sys
import os
import logging
from pprint import pprint
from dotenv import load_dotenv
import traceback

# Add project root
sys.path.append(os.getcwd())

# Standard load
load_dotenv()

from engine.exchange_interface import ExchangeInterface, normalize_symbol
# from config.settings import config # Conflict with raw loading sometimes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ForensicInvestigator")

def investigate_btc_usdc():
    print("--- FORENSIC ANALYSIS: BTC/USDC POSITION OWNER ---")
    try:
        # Initialize standard (will likely default to Demo/Testnet based on env or defaults)
        ex = ExchangeInterface(market_type='future')
        
        # 1. Get Current Position Details
        print("Fetching positions via ExchangeInterface (Raw)...")
        positions = ex.fetch_positions()
        if not positions:
            print("❌ No positions found (or Auth failed).")
            return

        target_pos = next((p for p in positions if 'BTC/USDC' in p['symbol'] or 'BTCUSDC' in p['symbol']), None)
        
        if not target_pos:
            print("❌ No active position found on BTC/USDC to investigate.")
            return

        print(f"found Position: {target_pos['side']} {target_pos['contracts']} @ {target_pos['entryPrice']}")
        
        # 2. Fetch Recent Trades using RAW REQUEST (Bypassing CCXT)
        print("\nFetching recent trades (RAW)...")
        
        # /fapi/v1/userTrades
        # Params: symbol, limit
        
        # Note: Raw Request requires normalized symbol.
        symbol_norm = normalize_symbol('BTC/USDC')
        
        trades = ex._raw_request('/fapi/v1/userTrades', params={'symbol': symbol_norm, 'limit': 50})
        
        if not trades:
            print("❌ Raw Trade Fetch returned empty/None.")
            return

        candidates = []
        for t in trades:
            # Raw trade format from Binance API
            # {'symbol': 'BTCUSDC', 'id': ..., 'orderId': ..., 'side': 'SELL', 'price': ..., 'qty': ..., 'realizedPnl': ..., 'marginAsset': 'USDC', 'quoteQty': ..., 'commission': ..., 'commissionAsset': 'USDC', 'time': ..., 'positionSide': 'BOTH', 'buyer': False, 'maker': False, 'isBuyer': False}
            
            # Note: Binance Raw userTrades does NOT include clientOrderId directly in all versions?
            # Wait, userTrades usually has 'orderId'. It MIGHT NOT have 'clientOrderId'.
            
            # If clientOrderId is missing, we might need to fetch the ORDER details.
            
            # Let's check keys
            # print(t.keys())
            
            # Only some endpoints return clientOrderId. userTrades might not.
            # We will use 'orderId' to fetch the order details if needed.
            
            cid = t.get('clientOrderId', '') # Try direct
            order_id = t.get('orderId')
            
            if not cid and order_id:
                # Need to fetch order to get CID
                 # Check if we can fetch order
                 # /fapi/v1/order
                 print(f"   Fetching details for Order ID {order_id}...")
                 order_details = ex._raw_request('/fapi/v1/order', params={'symbol': symbol_norm, 'orderId': order_id})
                 if order_details:
                     cid = order_details.get('clientOrderId', 'UNKNOWN')
            
            if 'CQB_' in cid:
                candidates.append({'type': 'BOT', 'cid': cid, 'trade': t})
            else:
                candidates.append({'type': 'MANUAL/EXTERNAL', 'cid': cid, 'trade': t})
                
        print(f"\nFound {len(candidates)} recent trades.")
        print("Most recent 5 trades w/ Client Order IDs:")
        for c in reversed(candidates[-5:]):
            t = c['trade']
            side = t.get('side', 'UNKNOWN')
            qty = t.get('qty', '0')
            price = t.get('price', '0')
            print(f"   [{c['type']}] {c['cid']} ({side} {qty} @ {price})")
            
    except Exception as e:
        print(f"❌ Error during investigation: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    investigate_btc_usdc()
