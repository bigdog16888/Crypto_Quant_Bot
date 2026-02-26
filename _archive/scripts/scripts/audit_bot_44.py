import sys
import os
import sqlite3
import json
import time

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.database import get_connection, get_bot_status
from engine.exchange_interface import ExchangeInterface

def audit_bot_44():
    print("🕵️ AUDIT BOT 44 (XAU/USDT)")
    print("="*60)
    
    # 1. DB State
    print("\n1️⃣  Database State:")
    status = get_bot_status(44)
    # Handle tuple return from get_bot_status (legacy fix) or dict
    # Current implementation returns dict, but let's be safe
    
    if status:
        print(f"   Status: {status.get('status', 'UNKNOWN')} (Active: {status.get('is_active')})")
        print(f"   Invested: ${status.get('total_invested', 0)}")
        print(f"   Step: {status.get('current_step')}")
        
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id=44")
        row = c.fetchone()
        if row:
            print(f"   Tracked Orders: Entry={row[0]}, TP={row[1]}")
    else:
        print("   ❌ Bot 44 not found in DB")

    # 2. Exchange State
    print("\n2️⃣  Exchange Orders (XAU/USDT):")
    try:
        ex = ExchangeInterface(market_type='future')
        # Force refresh to see reality
        orders = ex.fetch_open_orders('XAU/USDT', force_refresh=True)
        
        bot_orders = [o for o in orders if 'CQB_44_' in o.get('clientOrderId', '')]
        other_orders = [o for o in orders if 'CQB_44_' not in o.get('clientOrderId', '')]
        
        print(f"   Found {len(bot_orders)} orders for Bot 44.")
        print(f"   Found {len(other_orders)} orders for other bots.")
        
        if bot_orders:
            print("\n   --- Bot 44 Orders ---")
            for o in bot_orders:
                print(f"   [{o['id']}] {o['type']} {o['side']} {o['amount']} @ {o['price']} (CID: {o.get('clientOrderId')})")
                
            # Analyze CIDs
            cids = [o.get('clientOrderId') for o in bot_orders]
            unique_cids = set(cids)
            if len(cids) != len(unique_cids):
                print(f"\n   ⚠️  DUPLICATE CLIENT IDs DETECTED! (This shouldn't happen with Exchange Locking)")
            else:
                print(f"\n   ✅ All Client IDs are unique.")
                
            # Check for Multiple TPs
            tps = [o for o in bot_orders if '_TP_' in o.get('clientOrderId', '')]
            if len(tps) > 1:
                print(f"\n   🚨 CRITICAL: {len(tps)} TP ORDERS ACTIVE! (Should be 1)")
                for tp in tps:
                    print(f"      -> {tp['clientOrderId']}")
        
    except Exception as e:
        print(f"   ❌ Exchange Error: {e}")

if __name__ == "__main__":
    audit_bot_44()
