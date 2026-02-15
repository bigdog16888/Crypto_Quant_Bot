import os
import sys
import json
from collections import defaultdict
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection, get_bot_status

def garbage_collect():
    print("🧹 GARBAGE COLLECTOR: CLEANING ORPHAN ORDERS")
    print("="*60)
    
    # 1. IDENTIFY (Same logic as Scanner)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, pair, is_active FROM bots WHERE is_active = 1")
    active_bots = cursor.fetchall()
    
    active_pairs = set()
    pair_status_map = defaultdict(list)
    
    print(f"📋 DB State: Found {len(active_bots)} Active Bots.")
    
    for b in active_bots:
        bid, name, pair, active = b
        norm_pair = normalize_symbol(pair)
        active_pairs.add(norm_pair)
        
        trade_data = get_bot_status(bid)
        in_trade = False
        if trade_data and len(trade_data) > 4:
            in_trade = float(trade_data[4]) > 0
            
        pair_status_map[norm_pair].append({
            'id': bid,
            'name': name,
            'in_trade': in_trade
        })
    conn.close()

    print("\n📡 Fetching Orders...")
    ex = ExchangeInterface(market_type='future')
    
    all_orders = []
    try:
        try:
             all_orders = ex.exchange.fetch_open_orders()
        except:
             for pair in active_pairs:
                 orders = ex.exchange.fetch_open_orders(pair)
                 all_orders.extend(orders)
    except Exception as e:
        print(f"❌ Fetch Failed: {e}")
        return

    # 2. FILTER & COLLECT TRASH
    trash_bin = []
    
    print("\n🔍 ANALYZING OWNERSHIP (Strict verification against DB)...")
    print("-" * 60)
    
    for o in all_orders:
        sym = o.get('symbol')
        norm_sym = normalize_symbol(sym)
        oid = str(o.get('id'))
        
        reason = ""
        is_trash = False
        
        # 1. OWNERSHIP CHECK (Did WE create this?)
        conn = get_connection()
        cursor = conn.cursor()
        
        # Check bot_orders (Historical)
        cursor.execute("SELECT bot_id FROM bot_orders WHERE order_id = ?", (oid,))
        row = cursor.fetchone()
        owner_id = row[0] if row else None
        
        # Check trades (Active)
        if not owner_id:
             cursor.execute("SELECT bot_id FROM trades WHERE entry_order_id = ? OR tp_order_id = ?", (oid, oid))
             row = cursor.fetchone()
             owner_id = row[0] if row else None
        conn.close()
        
        if not owner_id:
            # UNKNOWN OWNER -> Manual/External -> IGNORE
            continue
        
        # KNOWN OWNER -> Check if stale
        trade_data = get_bot_status(owner_id)
        if not trade_data:
            # Bot deleted?
            is_trash = True
            reason = f"ORPHAN (Bot {owner_id} deleted)"
        else:
             in_trade = float(trade_data[4]) > 0
             if not in_trade:
                 is_trash = True
                 reason = f"STALE BOT ORDER (Bot {trade_data[1]} is IDLE)"
        
        if is_trash:
            trash_bin.append({
                'id': oid,
                'symbol': sym,
                'reason': reason,
                'desc': f"{o.get('side')} {o.get('type')} @ {o.get('price')}"
            })
            continue

        # 3. ACTIVE BOT ZOMBIE CHECK (The "Max 2" Enforcer)
        # If bot is Active, it should only have specific orders.
        # If we found an "Owned" order that is NOT in the "Active Set", it is a Zombie.
        
        # Get Active Set for this bot
        cursor = conn.cursor()
        cursor.execute("SELECT entry_order_id, tp_order_id FROM trades WHERE bot_id = ?", (owner_id,))
        row = cursor.fetchone()
        
        valid_ids = set()
        if row:
            if row[0]: valid_ids.add(str(row[0])) # Entry
            if row[1]: valid_ids.add(str(row[1])) # TP
            
        # Also allowed: OPEN grid orders in bot_orders
        cursor.execute("SELECT order_id FROM bot_orders WHERE bot_id = ? AND status='open' AND order_type='grid'", (owner_id,))
        for r in cursor.fetchall():
            valid_ids.add(str(r[0]))
            
        conn.close()
        
        if oid not in valid_ids:
             # It is OWNED, but not VALID/TRACKED. -> ZOMBIE!
             trash_bin.append({
                'id': oid,
                'symbol': sym,
                'reason': f"ZOMBIE GRID (Active Bot {owner_id} has excess order)",
                'desc': f"{o.get('side')} {o.get('type')} @ {o.get('price')}"
            })

    print(f"\n🗑️ FOUND {len(trash_bin)} ORDERS TO CLEAN:")
    for item in trash_bin:
        print(f"  - [{item['reason']}] {item['symbol']} | {item['desc']} (ID: {item['id']})")
        
    if not trash_bin:
        print("\n✅ NO TRASH FOUND. SYSTEM CLEAN.")
        return

    # 3. SAFETY CONFIRMATION (Automated for this task, but normally manual)
    # The user asked for a "Last Security Defense". 
    # We will proceed to cancel only confirmed Stale/Zombie orders.
    
    print("\n⚠️ PROCEEDING TO CANCEL...")
    time.sleep(2)
    
    cancelled_count = 0
    for item in trash_bin:
        try:
            print(f"  > Cancelling {item['id']}...")
            ex.exchange.cancel_order(item['id'], item['symbol'])
            cancelled_count += 1
        except Exception as e:
            print(f"  ❌ Failed: {e}")
            
    print(f"\n✨ DONE. Cancelled {cancelled_count} orphan orders.")

if __name__ == "__main__":
    garbage_collect()
