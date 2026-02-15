import sqlite3
import os
import sys
import pandas as pd

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def audit_orders():
    print("============================================================")
    print("EXCHANGE ORDER AUDIT")
    print("============================================================")
    
    # 1. Fetch from Exchange
    print("📡 Fetching ALL open orders from Exchange (Futures)...")
    try:
        ex = ExchangeInterface(market_type='future')
        # Fetch per symbol if fetch_open_orders doesn't support 'all' for this exchange, 
        # but Binance usually supports it or we iterate active symbols.
        # Check if we can fetch all.
        try:
             ex_orders = ex.exchange.fetch_open_orders()
        except Exception:
             # Fallback: iterate known symbols if generic fetch fails
             print("   ⚠️ Generic fetch failed, iterating known symbols...")
             symbols = config.ALLOWED_SYMBOLS
             ex_orders = []
             for sym in symbols:
                 try:
                     orders = ex.fetch_open_orders(sym)
                     ex_orders.extend(orders)
                 except: pass

        print(f"✅ Found {len(ex_orders)} Open Orders on Exchange.")
        
    except Exception as e:
        print(f"❌ Failed to fetch exchange orders: {e}")
        return

    # 2. Fetch from DB
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT order_id FROM bot_orders WHERE status='open'")
    db_order_ids = {str(row[0]) for row in cursor.fetchall()}
    conn.close()
    
    print(f"📊 Found {len(db_order_ids)} Open Orders in Database.")

    # 3. Compare
    orphans = []
    matched = 0
    
    for o in ex_orders:
        oid = str(o['id'])
        if oid in db_order_ids:
            matched += 1
        else:
            orphans.append(o)
            
    print(f"\n--- RESULTS ---")
    print(f"✅ Matched Orders: {matched}")
    print(f"⚠️ ORPHANED ORDERS (On Exchange, Not in DB): {len(orphans)}")
    
    if orphans:
        print("\n🔍 Orphan Details (First 10):")
        for o in orphans[:10]:
            print(f"   - [{o['symbol']}] {o['side'].upper()} {o['amount']} @ {o['price']} (ID: {o['id']})")
            
        if len(orphans) > 10:
            print(f"   ... and {len(orphans) - 10} more.")

if __name__ == "__main__":
    audit_orders()
