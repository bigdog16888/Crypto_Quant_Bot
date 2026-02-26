
import logging
import sys
import os
import time

sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OrderInspector")

def inspect_all():
    print("🔍 Inspecting ALL Active Bots & Orders...")
    
    # 1. Get Active Bots
    conn = get_connection()
    c = conn.cursor()
    # Fetch bots with status other than 'Stopped' or just get all for now to be safe
    c.execute("SELECT id, pair, status, name FROM bots WHERE status != 'Stopped'")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        print("❌ No active bots found in DB.")
        return

    # Convert to list of dicts for compatibility
    bots = [{'id': r[0], 'pair': r[1], 'status': r[2], 'name': r[3]} for r in rows]

    # Group by Pair
    pairs = set(b['pair'] for b in bots)
    
    print(f"📊 Active Pairs: {pairs}")
    
    ex = ExchangeInterface(market_type='future') 
    
    total_orders = 0
    
    for pair in pairs:
        print(f"\n🌍 Fetching Orders for {pair}...")
        try:
            orders = ex.fetch_open_orders(pair)
            print(f"   found {len(orders)} open orders.")
            total_orders += len(orders)
            
            for o in orders:
                # Basic Info
                oid = o['id']
                cid = o.get('clientOrderId', 'N/A')
                side = o['side']
                types = o['type']
                price = o['price']
                amount = o['amount']
                
                # Check ownership
                owner_str = "UNKNOWN"
                if cid.startswith('CQB_'):
                    parts = cid.split('_')
                    if len(parts) >= 2:
                        bot_id = parts[1]
                        owner_str = f"Bot {bot_id}"
                        
                print(f"   👉 [{owner_str}] ID:{oid} | {side.upper()} {types} @ {price} | Amt:{amount} | CID:{cid}")
                
        except Exception as e:
            print(f"❌ Error fetching {pair}: {e}")

    print(f"\n✅ Total Exchange Orders Found: {total_orders}")

if __name__ == "__main__":
    inspect_all()
