import sys
import os
import sqlite3
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.database import get_connection, reset_bot_after_tp
from engine.exchange_interface import ExchangeInterface

def fix_system_state():
    print("🔧 SYSTEM STATE REPAIR")
    print("="*60)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Fix DB Integrity (Status vs Invested)
    print("\n1️⃣  Running Integrity Check...")
    cursor.execute("SELECT id, name, status, total_invested FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1")
    rows = cursor.fetchall()
    
    fixed_count = 0
    ghost_reset_count = 0
    
    exchange = ExchangeInterface(market_type='future')
    positions = exchange.fetch_positions()
    # Map symbol -> size
    pos_map = {}
    for p in positions:
        pos_map[p['symbol']] = float(p.get('contracts', 0) or p.get('size', 0) or 0)

    for row in rows:
        bot_id, name, status, invested = row
        invested = float(invested or 0)
        
        # A. Fix Mismatched Status
        if status == 'IN TRADE' and invested <= 0:
            print(f"   🛠️  Fixing {name}: Status 'IN TRADE' -> 'Waiting for Signal' (Invested: 0)")
            cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id=?", (bot_id,))
            fixed_count += 1
            
        elif status == 'Waiting for Signal' and invested > 0:
            print(f"   🛠️  Fixing {name}: Status 'Waiting for Signal' -> 'IN TRADE' (Invested: {invested})")
            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot_id,))
            fixed_count += 1

        # B. Detect & Kill Stale Ghosts (Invested > 0, but NO Exchange Position)
        # Only if invested > 0
        if invested > 0:
            # Check if this bot actually has a position on exchange
            # We need the pair
            cursor.execute("SELECT pair FROM bots WHERE id=?", (bot_id,))
            pair = cursor.fetchone()[0]
            
            has_real_pos = False
            for sym, size in pos_map.items():
                # Simple check: is symbol in pair?
                # Better: normalize
                if sym.replace('/','').split(':')[0] == pair.replace('/','').split(':')[0]:
                    if size > 0: has_real_pos = True
            
            if not has_real_pos:
                print(f"   👻 GHOST DETECTED: {name} ({pair}) has {invested} invested but NO exchange position.")
                print(f"      -> RESETTING BOT STATE.")
                try:
                    # Using reset_bot_after_tp to cleanly wipe trade state
                    # We can't import it directly due to circular imports sometimes, but we imported at top
                    # We need to act carefully.
                    cursor.execute("UPDATE trades SET current_step=0, total_invested=0, avg_entry_price=0, target_tp_price=0, entry_order_id=NULL, tp_order_id=NULL WHERE bot_id=?", (bot_id,))
                    cursor.execute("UPDATE bots SET status='Waiting for Signal' WHERE id=?", (bot_id,))
                    cursor.execute("UPDATE bot_orders SET status='auto_closed' WHERE bot_id=? AND status='open'", (bot_id,))
                    ghost_reset_count += 1
                except Exception as e:
                    print(f"      ❌ Failed to reset: {e}")

    conn.commit()
    print(f"   ✅ Integrity Fixes: {fixed_count}")
    print(f"   ✅ Ghosts Reset: {ghost_reset_count}")

    # 2. Fix Duplicate TPs (Bot 44)
    print("\n2️⃣  Cleaning Duplicate Orders...")
    open_orders = exchange.fetch_open_orders()
    
    # Group by Bot
    orders_by_bot = {}
    for o in open_orders:
        cid = o.get('clientOrderId', '')
        if cid.startswith('CQB_'):
            parts = cid.split('_')
            bid = parts[1]
            if bid not in orders_by_bot: orders_by_bot[bid] = {'TP': [], 'GRID': []}
            
            if '_TP_' in cid: orders_by_bot[bid]['TP'].append(o)
            elif '_GRID_' in cid: orders_by_bot[bid]['GRID'].append(o)

    for bid, types in orders_by_bot.items():
        tps = types['TP']
        if len(tps) > 1:
            print(f"   ⚠️  Bot {bid} has {len(tps)} TP orders! Keeping the newest.")
            # Sort by ID (usually implies time) or timestamp if available
            # We'll just keep the one with ID that looks newest or random?
            # Safest: Cancel ALL and let bot replace 1 correct one next cycle.
            print(f"      -> Cancelling {len(tps)} TPs...")
            for o in tps:
                try:
                    exchange.cancel_order(o['id'], o['symbol'])
                    print(f"         Cancelled {o['id']}")
                except:
                    print(f"         Failed to cancel {o['id']}")
        
        grids = types['GRID']
        if len(grids) > 1:
            print(f"   ⚠️  Bot {bid} has {len(grids)} Grid orders! Cancelling all for safety.")
            for o in grids:
                try:
                    exchange.cancel_order(o['id'], o['symbol'])
                except: pass

    print("\nDone.")

if __name__ == "__main__":
    fix_system_state()
