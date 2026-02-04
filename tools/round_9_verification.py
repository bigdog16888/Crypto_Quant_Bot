import sys
import os
import sqlite3
import json
import time

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def verify_round_9():
    print("="*60)
    print("ROUND 9: DEEP SYSTEM VERIFICATION")
    print("="*60)
    
    # 1. Connect to DB
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 2. Get Active Bots
    cursor.execute("SELECT id, name, pair, config, is_active FROM bots WHERE is_active=1")
    active_bots = cursor.fetchall()
    print(f"\n>>> 1. ACTIVE BOTS (DB): {len(active_bots)}")
    
    bot_map = {} # {id: {name, pair, config}}
    for b in active_bots:
        bot_map[b[0]] = {'name': b[1], 'pair': b[2], 'config': b[3]}
        print(f" - [{b[0]}] {b[1]} ({b[2]})")
        
    # 3. Get Bots In Trade
    cursor.execute("""
        SELECT t.bot_id, t.total_invested, t.current_step 
        FROM trades t 
        JOIN bots b ON t.bot_id = b.id 
        WHERE b.is_active=1 AND t.total_invested > 0
    """)
    triggered_bots = cursor.fetchall()
    print(f"\n>>> 2. TRIGGERED BOTS (In Trade per DB): {len(triggered_bots)}")
    
    triggered_ids = []
    for t in triggered_bots:
        bid = t[0]
        triggered_ids.append(bid)
        b_info = bot_map.get(bid, {'name': 'Unknown'})
        print(f" - [{bid}] {b_info['name']} | Invested: ${t[1]:.2f} | Step: {t[2]}")

    # 4. Fetch Real Exchange State
    print("\n>>> 3. EXCHANGE CROSS-CHECK (Fetching Data...)")
    try:
        # Assuming Futures for now as per context
        exchange = ExchangeInterface(market_type='future')
        
        # A. Positions
        positions = exchange.fetch_positions()
        pos_map = {} # {symbol: quantity}
        for p in positions:
            sym = p.get('symbol').replace('/','')
            qty = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if qty != 0:
                pos_map[sym] = qty
                
        # B. Open Orders
        open_orders = exchange.fetch_open_orders()
        order_map = {} # {symbol: [orders]}
        for o in open_orders:
            sym = o.get('symbol').replace('/','')
            if sym not in order_map: order_map[sym] = []
            order_map[sym].append({
                'id': o.get('id'),
                'type': o.get('type'),
                'side': o.get('side'),
                'price': o.get('price'),
                'amount': o.get('amount')
            })
            
    except Exception as e:
        print(f"!!! CRITICAL: Failed to connect to exchange: {e}")
        return

    # 5. Compare
    print("\n>>> 4. RECONCILIATION REPORT")
    
    for bid in triggered_ids:
        b = bot_map[bid]
        pair = b['pair']
        sym = pair.replace('/','').split(':')[0]
        
        print(f"\n--- Checking Bot {bid}: {b['name']} ({pair}) ---")
        
        # Check Position
        db_has_pos = True # Based on triggered list
        ex_pos_qty = pos_map.get(sym, 0)
        
        if ex_pos_qty != 0:
            print(f" ✅ Position MATCH: Exchange has {ex_pos_qty} contracts.")
        else:
            print(f" ❌ Position MISMATCH: DB says In Trade, Exchange has NO position!")
            
        # Check Orders (TP / Grid)
        print(" [Checking Orders]")
        # DB Orders
        cursor.execute("SELECT order_id, order_type, price, status FROM bot_orders WHERE bot_id=? AND status='open'", (bid,))
        db_orders = cursor.fetchall()
        
        ex_orders = order_map.get(sym, [])
        ex_order_ids = [str(o['id']) for o in ex_orders]
        
        tp_found = False
        grid_found = False
        
        if not db_orders:
            print(" ⚠️ DB shows NO open orders (Expected TP + Grid?)")
        
        for dbo in db_orders:
            oid, otype, oprice, ostatus = dbo
            on_ex = str(oid) in ex_order_ids
            status_icon = "✅" if on_ex else "❌"
            msg = "Found on Exchange" if on_ex else "MISSING on Exchange"
            print(f"   - DB [{otype}] {oid} @ {oprice}: {status_icon} {msg}")
            
            if on_ex:
                if otype == 'tp': tp_found = True
                if otype == 'grid': grid_found = True
        
        # Check for untracked orders
        db_oids = [str(o[0]) for o in db_orders]
        for exo in ex_orders:
            if str(exo['id']) not in db_oids:
                print(f"   ⚠️ UNTRACKED Exchange Order: {exo['type']} {exo['side']} @ {exo['price']} (ID: {exo['id']})")
        
        if tp_found and grid_found:
             print(" ✅ Order Structure: OK (Both TP and Grid Verified)")
        elif tp_found:
             print(" ⚠️ Order Structure: Partial (TP only)")
        elif grid_found:
             print(" ⚠️ Order Structure: Partial (Grid only)")
        else:
             print(" ❌ Order Structure: CRITICAL FAILURE (No valid orders found)")

    # 6. Check for Zombie Positions (Exchange has pos, DB doesn't know)
    print("\n>>> 5. ZOMBIE SCAN")
    active_pairs = [b['pair'].replace('/','').split(':')[0] for b in bot_map.values()]
    triggered_pairs = [bot_map[bid]['pair'].replace('/','').split(':')[0] for bid in triggered_ids]
    
    for sym, qty in pos_map.items():
        if sym not in triggered_pairs:
             print(f" 🧟 ZOMBIE POS DETECTED: {sym} has {qty} contracts, but no Bot is 'In Trade' for it!")
             # Is it at least an active bot?
             if sym in active_pairs:
                 print("    (Bot is Active, but DB says 'Not In Trade' -> Sync Error)")
             else:
                 print("    (No Active Bot for this pair -> rogue position)")
    
    conn.close()
    print("\nRound 9 Complete.")

if __name__ == "__main__":
    verify_round_9()
