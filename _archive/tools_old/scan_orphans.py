import os
import sys
import json
from collections import defaultdict

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection

def scan_for_orphans():
    print("🛡️ ORPHAN ORDER SCANNER: THE 'LAST DEFENSE' AUDIT")
    print("="*60)
    
    # 1. Get Trusted DB State (Who SHOULD have orders?)
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all bots that are marked ACTIVE
    cursor.execute("SELECT id, name, pair, is_active FROM bots WHERE is_active = 1")
    active_bots = cursor.fetchall()
    
    # Get all bots currently IN TRADE (according to DB)
    cursor.execute("SELECT bot_id FROM trade_history WHERE action != 'SELL' GROUP BY bot_id HAVING count(*) % 2 != 0")
    # Actually, better to check 'trades' table or status if available, 
    # but let's stick to the 'bots' table 'total_invested' check if possible.
    # Simpler: Get active pairs.
    
    active_pairs = set()
    active_bot_map = defaultdict(list)
    
    print(f"📋 DB State: Found {len(active_bots)} Active Bots.")
    for b in active_bots:
        bid, name, pair, active = b
        norm_pair = normalize_symbol(pair)
        active_pairs.add(norm_pair)
        active_bot_map[norm_pair].append(name)
        # print(f"  - [{bid}] {name} on {pair}")

    conn.close()

    # 2. Get ACTUAL Exchange State (What is sitting there?)
    print("\n📡 Scanning Exchange for ALL Open Orders...")
    ex = ExchangeInterface(market_type='future')
    
    # We need to fetch ALL open orders. 
    # Ideally fetch_open_orders() without symbol gets all, but some exchanges require symbol.
    # We will try to fetch all if supported, or iterate known markets.
    
    all_orders = []
    try:
        # CCXT 'fetch_open_orders' without symbol is not always supported for all exchanges/markets 
        # but for Binance Futures it usually works or we iterate.
        # Let's try iterating active pairs + a few majors just in case, 
        # OR better: use fetch_positions to key off active markets, 
        # BUT that misses orders on pairs with no position.
        
        # Safer strategy: Get all markets, then scan. (Can be slow).
        # Optimization: Scan 'active_pairs' first, then report. 
        # User said "plenty of open orders", implying they are visible.
        # They are likely on the pairs we know about.
        
        # Let's Scan ALL Open Orders if possible
        try:
            all_orders = ex.exchange.fetch_open_orders()
        except:
            print("  ! Full scan not supported, iterating active symbols...")
            for pair in active_pairs:
                orders = ex.exchange.fetch_open_orders(pair)
                all_orders.extend(orders)
                
    except Exception as e:
        print(f"❌ Scan Failed: {e}")
        return

    print(f"🔎 Found {len(all_orders)} Total Open Orders on Exchange.")
    
    # 3. The Litmus Test (Is it Orphaned?)
    orphans = []
    legit = 0
    
    print("\n💀 POTENTIAL ORPHANS DETECTED:")
    print("-" * 60)
    
    # PRE-FETCH: Get current status of all matching bots to see if they are IDLE
    # Map Normalized Pair -> List of (BotID, BotName, IsInTrade)
    pair_status_map = defaultdict(list)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    for b in active_bots:
        bid, name, pair, active = b
        norm_pair = normalize_symbol(pair)
        
        # Check if actually in trade
        # Use helper from database module to be safe about schema
        from engine.database import get_bot_status
        trade_data = get_bot_status(bid)
        # trade_data format: (id, name, pair, current_step, total_invested, ...)
        # Index 3 is total_invested (actually index 3 in get_bot_status is total_invested?? No, wait.)
        # get_bot_status returns: (id, name, pair, current_step, total_invested, avg_price, tp, last_exit, start_time)
        # So total_invested is index 4.
        
        in_trade = False
        if trade_data and len(trade_data) > 4:
            in_trade = float(trade_data[4]) > 0 # Index 4 is total_invested
        
        pair_status_map[norm_pair].append({
            'id': bid,
            'name': name,
            'in_trade': in_trade
        })
    conn.close()

    # PRE-FETCH: Cache all verified bot order IDs ?? 
    # Actually, singular lookups are safer for 10-20 orders.
    # But fast enough.
    
    print("\n🔍 ANALYZING OWNERSHIP (Strict verification against DB)...")
    print("-" * 60)

    for o in all_orders:
        sym = o.get('symbol')
        norm_sym = normalize_symbol(sym)
        oid = str(o.get('id'))
        otype = o.get('type')
        side = o.get('side')
        price = o.get('price')
        
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
        
        desc = f"{sym} | {side} {otype} @ {price} | ID: {oid}"
        
        if not owner_id:
            # UNKNOWN OWNER -> Manual/External
            print(f"  🛑 EXTERNAL/MANUAL ORDER: {desc}")
            print(f"     (Ignored. Not in Bot DB.)")
            legit += 1 # Treated as 'legit' in sense of 'do not touch'
        else:
            # KNOWN OWNER -> Check if valid
            # Get Bot Status
            trade_data = get_bot_status(owner_id)
            # (id, name, pair, current_step, total_invested, ...)
            
            if not trade_data:
                 print(f"  ⚠️ ORPHAN (Deleted Bot): {desc}")
                 print(f"     (Bot {owner_id} no longer exists!)")
                 orphans.append(o)
            else:
                 bot_name = trade_data[1]
                 in_trade = float(trade_data[4]) > 0
                 
                 if in_trade:
                      # Valid active order
                      # print(f"  ✅ VALID: {desc} (Owner: {bot_name})")
                      legit += 1
                 else:
                      # Bot is Idle, but has order -> STALE
                      print(f"  ⚠️ STALE BOT ORDER: {desc}")
                      print(f"     (Owner: {bot_name} is IDLE/SCANNING)")
                      orphans.append(o)
            
    print("-" * 60)
    print(f"📊 Summary:")
    print(f"  ✅ Legitimate Orders (Active Pairs): {legit}")
    print(f"  ⚠️ Orphan Orders (Zombie Pairs): {len(orphans)}")
    
    if len(orphans) > 0:
        print("\n🛡️ RECOMMENDATION: These orders belong to NO active bot. They should be cancelled immediately.")
        
if __name__ == "__main__":
    scan_for_orphans()
