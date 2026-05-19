
import sqlite3
import time
import sys
import os
import json

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import DB_PATH

def get_conn():
    return sqlite3.connect(DB_PATH, timeout=10)

def run_live_verification():
    print("🚀 STARTING LIVE VERIFICATION - TESTNET")
    conn = get_conn()
    cursor = conn.cursor()

    # 1. Get Active Bots
    cursor.execute("SELECT id, name, pair, direction, rsi_limit FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    
    if len(bots) < 2:
        print(f"❌ Need at least 2 active bots for this test. Found {len(bots)}.")
        return

    print(f"📋 Found {len(bots)} Active Bots:")
    original_configs = {}
    
    for b in bots:
        bid, name, pair, direction, rsi = b
        print(f"   - Bot {bid}: {name} [{pair} {direction}] (RSI: {rsi})")
        original_configs[bid] = rsi

    # 2. Force Entry
    print("\n⚡ FORCING ENTRY (Adjusting RSI Thresholds)...")
    for b in bots:
        bid, name, pair, direction, rsi = b
        
        # If LONG, set RSI limit to 100 (Price is always < 100 RSI, so Trigger)
        # If SHORT, set RSI limit to 0 (Price is always > 0 RSI, so Trigger)
        # Wait... RSI strategy usually works:
        # Long: RSI < Limit
        # Short: RSI > Limit
        
        target_rsi = 100 if direction.upper() == 'LONG' else 0
        
        print(f"   -> Setting Bot {bid} ({direction}) RSI Limit to {target_rsi}")
        cursor.execute("UPDATE bots SET rsi_limit = ? WHERE id = ?", (target_rsi, bid))
        
    conn.commit()
    
    # 3. Wait for Trades
    print("\n⏳ Waiting for Runner to pick up changes and enter trades (Max 60s)...")
    start_wait = time.time()
    bots_in_trade = set()
    
    while time.time() - start_wait < 60:
        cursor.execute("SELECT bot_id FROM trades WHERE total_invested > 0")
        active_trade_rows = cursor.fetchall()
        current_active = {r[0] for r in active_trade_rows}
        
        if len(current_active) >= len(bots):
            bots_in_trade = current_active
            print(f"   ✅ All {len(bots)} bots entered trade!")
            break
            
        time.sleep(2)
        print(f"   ... Waiting ({len(current_active)}/{len(bots)} in trade)")
        
    if len(bots_in_trade) < len(bots):
        print("   ⚠️ Timed out waiting for all bots. Proceeding with what we have.")

    # 4. Verify State
    print("\n🔍 VERIFYING FUNDAMENTAL RESULTS:")
    
    # Check Orders per Bot
    all_passed = True
    for bid in original_configs.keys():
        print(f"\n   🤖 Bot {bid}:")
        
        # Check Status
        cursor.execute("SELECT status FROM bots WHERE id=?", (bid,))
        status = cursor.fetchone()[0]
        print(f"      Status: {status}")
        
        # Check Orders
        cursor.execute("SELECT order_type, status, price, amount FROM bot_orders WHERE bot_id=? AND status='open'", (bid,))
        orders = cursor.fetchall()
        
        tp_orders = [o for o in orders if o[0] == 'tp']
        grid_orders = [o for o in orders if o[0] == 'grid']
        
        print(f"      Open Orders: {len(orders)} (Expected 2)")
        if len(tp_orders) == 1:
            print(f"      ✅ TP Order: Found @ {tp_orders[0][2]}")
        else:
            print(f"      ❌ TP Order: MISSING or Duplicate ({len(tp_orders)})")
            all_passed = False
            
        if len(grid_orders) >= 1:
             print(f"      ✅ Grid Order: Found @ {grid_orders[0][2]}")
        else:
             print(f"      ❌ Grid Order: MISSING")
             all_passed = False

    # Check Global Sync
    print("\n   ⚖️  Global Reconciliation:")
    cursor.execute("SELECT SUM(t.total_invested / t.avg_entry_price) FROM trades t JOIN bots b ON t.bot_id = b.id WHERE b.direction='LONG'")
    virtual_long = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(t.total_invested / t.avg_entry_price) FROM trades t JOIN bots b ON t.bot_id = b.id WHERE b.direction='SHORT'")
    virtual_short = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT SUM(size) FROM active_positions WHERE side='LONG'")
    physical_long = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(size) FROM active_positions WHERE side='SHORT'")
    physical_short = cursor.fetchone()[0] or 0
    
    print(f"      Virtual Long: {virtual_long:.4f} | Physical Long: {physical_long:.4f}")
    
    # 5. Restore Config
    print("\n🔄 Restoring Original Configs...")
    for bid, rsi in original_configs.items():
        cursor.execute("UPDATE bots SET rsi_limit = ? WHERE id = ?", (rsi, bid))
    conn.commit()
    print("   ✅ Configs Restored.")
    
    conn.close()

if __name__ == "__main__":
    run_live_verification()
