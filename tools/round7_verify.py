import os
import sys
import time
import sqlite3
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection
from config.settings import config

def round7_verify():
    print("🚀 STARTING ROUND 7 VERIFICATION: CRASH RECOVERY & STABILITY")
    print("="*60)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Database State
    cursor.execute("SELECT COUNT(*) FROM bots WHERE is_active = 1")
    active_bots_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT t.*, b.name, b.pair FROM trades t JOIN bots b ON t.bot_id = b.id")
    trades = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
    
    print(f"📊 DB State: {active_bots_count} Active Bots, {len(trades)} Bots in Trade.")

    # 2. Exchange State
    ex = ExchangeInterface(market_type='future')
    all_positions = []
    try:
        all_positions = ex.fetch_positions()
    except Exception as e:
        print(f"❌ Failed to fetch positions: {e}")
        
    open_positions = [p for p in all_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) != 0]
    print(f"📡 Exchange State: {len(open_positions)} Open Positions found.")

    # 3. Discrepancy Check
    print("\n🔍 DISCREPANCY CHECK:")
    
    trade_pairs_norm = {normalize_symbol(t['pair']): t['pair'] for t in trades}
    pos_pairs_norm = {normalize_symbol(p['symbol']): p['symbol'] for p in open_positions}
    
    # Bots in trade but no position
    for norm, orig in trade_pairs_norm.items():
        if norm not in pos_pairs_norm:
            print(f"  ⚠️ Warning: Bot in trade for {orig} but no matching exchange position!")
        
    # Positions with no bot
    for norm, orig in pos_pairs_norm.items():
        if norm not in trade_pairs_norm:
            print(f"  ⚠️ Warning: Exchange position for {orig} but no matching bot in trade!")

    # 4. Order Health
    print("\n📦 ORDER HEALTH:")
    for t in trades:
        try:
            orders = ex.fetch_open_orders(t['pair'])
            
            # TP detection: Check for 'reduceOnly' in info OR 'close' in side/type (ccxt dependent)
            tp_orders = []
            for o in orders:
                info = o.get('info', {})
                # Binance specific reduceOnly check
                is_reduce = info.get('reduceOnly') == True or info.get('reduceOnly') == 'true'
                if is_reduce:
                    tp_orders.append(o)
            
            grid_orders = [o for o in orders if o not in tp_orders]
            
            print(f"  [{t['name']} | {t['pair']}]: {len(tp_orders)} TP, {len(grid_orders)} Grid")
            if not tp_orders:
                print(f"    ❌ CRITICAL: No Take Profit order found for {t['pair']}!")
        except Exception as e:
            print(f"    ⚠️ Error checking orders for {t['pair']}: {e}")

    # 5. Log Audit
    print("\n📜 LOG AUDIT (Last 15 minutes):")
    log_file = config.PATHS["LOG_FILE"]
    if os.path.exists(log_file):
        # Look for ERRORS or WARNINGS in the last 1000 lines
        cmd = f'powershell -Command "Get-Content {log_file} -Tail 1000 | Select-String -Pattern \'ERROR\', \'WARNING\'"'
        result = os.popen(cmd).read()
        if result.strip():
            print("  ⚠️ RECENT LOG ISSUES FOUND:")
            # Filter out known benign warnings if any
            lines = result.strip().split('\n')
            for line in lines[-10:]: # Show last 10
                print(f"    {line.strip()}")
        else:
            print("  ✅ No recent ERRORS or WARNINGS in logs.")
    else:
        print("  ⚠️ Log file not found.")

    print("\n" + "="*60)
    print("✅ ROUND 7 VERIFICATION COMPLETE")

if __name__ == "__main__":
    round7_verify()
