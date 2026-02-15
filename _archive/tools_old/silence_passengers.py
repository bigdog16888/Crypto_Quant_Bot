import sqlite3
import os
import sys

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

def silence_passengers():
    print("🤫 SILENCING PASSENGERS...")
    print("==========================")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Identify Owners (Hardcoded for safety based on known state)
    owners = [43, 44]
    
    # 2. Reset Everyone Else
    print("   Resetting DB state for non-owners...")
    cursor.execute(f"""
        UPDATE trades 
        SET total_invested=0, current_step=0, entry_confirmed=0
        WHERE bot_id NOT IN ({','.join(map(str, owners))})
        AND total_invested > 0
    """)
    print(f"   ✅ Reset {cursor.rowcount} passenger bots to IDLE.")
    
    conn.commit()
    conn.close()
    
    # 3. Cancel Orphan Orders
    print("   Cancelling leftover orders...")
    try:
        exchange = ExchangeInterface(market_type='future')
        orders = exchange.fetch_open_orders()
        
        count = 0
        for o in orders:
            cid = o.get('clientOrderId', '')
            # If order belongs to a non-owner bot
            if 'CQB_' in cid:
                try:
                    bot_id = int(cid.split('_')[1])
                    if bot_id not in owners:
                        print(f"   ❌ Cancelling {cid} (Bot {bot_id})")
                        exchange.cancel_order(o['id'], o['symbol'])
                        count += 1
                except: pass
                
        print(f"   ✅ Cancelled {count} passenger orders.")
        
    except Exception as e:
        print(f"   ⚠️ Exchange Cleanup Error: {e}")

    print("==========================")

if __name__ == "__main__":
    silence_passengers()
