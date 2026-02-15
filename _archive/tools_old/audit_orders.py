import sys
import os
from tabulate import tabulate

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

def audit_orders():
    print("🛡️ AUDITING OPEN ORDERS 🛡️")
    print("===========================")
    
    # 1. Get Exchange Orders
    try:
        exchange = ExchangeInterface(market_type='future')
        orders = exchange.fetch_open_orders()
    except Exception as e:
        print(f"❌ Exchange Error: {e}")
        return

    if not orders:
        print("   👉 STATUS: NO OPEN ORDERS (Positions are 'Naked')")
    else:
        data = []
        for o in orders:
            # Parse Deterministic ID if present
            cid = o.get('clientOrderId', '')
            tag = "Legacy/Manual"
            if 'CQB' in cid:
                parts = cid.split('_')
                if len(parts) >= 3:
                    bot_id = parts[1]
                    type_str = parts[2]
                    tag = f"🤖 Bot {bot_id} {type_str}"
            
            data.append([
                o['symbol'], 
                o['side'].upper(), 
                o['type'].upper(), 
                f"{float(o['amount']):.4f}", 
                f"${float(o['price']):.2f}", 
                tag
            ])
            
        print(tabulate(data, headers=["Symbol", "Side", "Type", "Qty", "Price", "Source"], tablefmt="grid"))
        print(f"   ✅ Total Orders: {len(orders)}")

    print("===========================")

if __name__ == "__main__":
    audit_orders()
