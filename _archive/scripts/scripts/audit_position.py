import sys
import os
import sqlite3
import time
import argparse
import logging
from datetime import datetime
from typing import Dict, List, Optional

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config
from engine.database import get_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger("AuditPosition")

def setup_args():
    parser = argparse.ArgumentParser(description="Audit and repair bot state from exchange history.")
    parser.add_argument("--bot_id", type=int, help="Specific Bot ID to audit (optional)")
    parser.add_argument("--fix", action="store_true", help="Apply fixes to the database")
    parser.add_argument("--limit", type=int, default=50, help="Number of history orders to fetch (default: 50)")
    parser.add_argument("--days", type=int, default=7, help="Days of history to scan (default: 7)")
    return parser.parse_args()

def get_db_bot_state(bot_id: int):
    """Fetches current DB state for a bot."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT b.id, b.name, b.pair, b.strategy_type, b.direction,
               t.total_invested, t.current_step, t.avg_entry_price, t.basket_start_time
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.id = ?
    """, (bot_id,))
    
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
        
    return {
        'id': row[0],
        'name': row[1],
        'pair': row[2],
        'strategy': row[3],
        'direction': row[4],
        'invested': row[5] or 0.0,
        'step': row[6] or 0,
        'avg_price': row[7] or 0.0,
        'start_time': row[8] or 0
    }

def reconstruct_state(orders: List[dict]):
    """
    Replays order history to reconstruct state.
    Assumes chronological order (Oldest -> Newest).
    """
    state = {
        'invested': 0.0,
        'size': 0.0,
        'step': 0,
        'avg_price': 0.0,
        'last_action_time': 0
    }
    
    # Sort by timestamp just in case
    sorted_orders = sorted(orders, key=lambda x: x['timestamp'])
    
    print("\n   📜 History Replay:")
    
    for o in sorted_orders:
        cid = o.get('clientOrderId', '')
        # Parse CID: CQB_{bot_id}_{type}_{step}
        if not cid.startswith('CQB_'): continue
        
        parts = cid.split('_')
        if len(parts) < 3: continue
        
        o_type = parts[2] # ENTRY, GRID, TP, SL
        
        # Parse logic
        filled = float(o['amount'])
        price = float(o['average'] or o['price'])
        cost = filled * price
        ts = datetime.fromtimestamp(o['timestamp']/1000).strftime('%Y-%m-%d %H:%M')
        
        print(f"      [{ts}] {o_type:<8} | Size: {filled:.4f} @ {price:.2f} | Cost: ${cost:.2f}")

        if o_type == 'ENTRY':
            # Entry resets the basket (unless we are adding to existing?)
            # Usually Entry is step 1.
            # But if we were already in trade, this might be a second entry? 
            # For martingale, ENTRY usually implies Start.
            # However, safety check: if we are closed, we reset.
            if state['size'] < 0.0001: 
                # Fresh start
                state['invested'] = cost
                state['size'] = filled
                state['step'] = 1
                state['last_action_time'] = o['timestamp']
            else:
                # Adding to existing (shouldn't happen for ENTRY usually, but handle linear)
                 state['invested'] += cost
                 state['size'] += filled
                 # Step? Entry is usually step 1.
        
        elif o_type == 'GRID':
            step_num = int(parts[3]) if len(parts) > 3 else state['step'] + 1
            state['invested'] += cost
            state['size'] += filled
            state['step'] = max(state['step'], step_num)
            state['last_action_time'] = o['timestamp']
            
        elif o_type in ['TP', 'SL', 'MANUAL']:
            # Reducing position
            # Pro-rata reduction of invested?? 
            # Or FIFO?
            # For PnL calc, we usually care about Avg Entry.
            # If we sell half, invested reduces by half?
            
            if state['size'] > 0:
                fraction = filled / state['size']
                if fraction > 0.99: # Full close
                    state['invested'] = 0.0
                    state['size'] = 0.0
                    state['step'] = 0
                    state['avg_price'] = 0.0
                    print("      ---> POSITION CLOSED")
                else:
                    # Partial
                    state['invested'] *= (1 - fraction)
                    state['size'] -= filled
                    print(f"      ---> Partial Close ({fraction*100:.1f}%)")
            
        # Recalc Avg
        if state['size'] > 0:
            state['avg_price'] = state['invested'] / state['size']
            
    return state

def audit_bot(bot_id: int, exchange: ExchangeInterface, args):
    db_state = get_db_bot_state(bot_id)
    if not db_state:
        print(f"❌ Bot {bot_id} not found in DB.")
        return

    print(f"\n🔎 AUDITING BOT {bot_id}: {db_state['name']} ({db_state['pair']})")
    print("-" * 60)
    
    # 1. Fetch History
    # We look back X days
    start_ts = int((time.time() - (args.days * 86400)) * 1000)
    history = exchange.fetch_closed_orders(db_state['pair'], since=start_ts, limit=args.limit)
    
    # Filter for this bot
    my_orders = [o for o in history if f"CQB_{bot_id}_" in o.get('clientOrderId', '') and o['status'] == 'filled']
    
    if not my_orders:
        print(f"   ⚠️ No trade history found in the last {args.days} days.")
        # If DB says we are in trade, this is suspicious? Or maybe trade is older than 7 days.
        if db_state['invested'] > 0:
             print("   ⚠️ DB says IN TRADE, but no recent history. Position might be old or Phantom.")
             # Fallthrough to fix logic (Real State = 0)
        else:
             print("   ✅ DB says IDLE. History matches (Empty).")
             return

    # 2. Reconstruct
    real_state = reconstruct_state(my_orders)
    
    # 3. Compare
    print("-" * 60)
    print(f"   {'METRIC':<15} | {'DB STATE':<15} | {'REALITY (Audit)':<15} | {'DIFF':<15}")
    print("-" * 60)
    
    diff_invested = abs(db_state['invested'] - real_state['invested'])
    diff_step = db_state['step'] - real_state['step']
    
    cols = [
        ('Invested ($)', db_state['invested'], real_state['invested'], diff_invested),
        ('Step', db_state['step'], real_state['step'], diff_step),
        ('Avg Price', db_state['avg_price'], real_state['avg_price'], abs(db_state['avg_price'] - real_state['avg_price'])),
    ]
    
    has_mismatch = False
    for label, db_val, real_val, diff in cols:
        flag = ""
        is_diff = False
        
        if label == 'Step':
             if diff != 0: is_diff = True
        else:
             if diff > 0.01: is_diff = True
             
        if is_diff:
            flag = "❌ MISMATCH"
            has_mismatch = True
        else:
            flag = "✅ OK"
            
        print(f"   {label:<15} | {db_val:<15.4f} | {real_val:<15.4f} | {flag}")
        
    # 4. Fix
    if has_mismatch and args.fix:
        print("\n🔧 FIXING DATABASE...")
        conn = get_connection()
        cursor = conn.cursor()
        
        # If reality says 0 invested, we reset
        if real_state['invested'] < 1.0:
             print("   -> Resetting to IDLE (Scanning)")
             cursor.execute("""
                UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0 
                WHERE bot_id=?
             """, (bot_id,))
             cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
        else:
             print(f"   -> Updating State: Invested={real_state['invested']:.2f}, Step={real_state['step']}")
             cursor.execute("""
                UPDATE trades SET total_invested=?, current_step=?, avg_entry_price=?, entry_confirmed=1
                WHERE bot_id=?
             """, (real_state['invested'], real_state['step'], real_state['avg_price'], bot_id))
             cursor.execute("UPDATE bots SET status='In Trade' WHERE id=?", (bot_id,))
             
        conn.commit()
        conn.close()
        print("✅ Database Updated.")
        
    elif has_mismatch:
        print("\n⚠️ Mismatch detected. Run with --fix to repair.")

def main():
    args = setup_args()
    
    print("🚀 STARTING AUDIT...")
    exchange = ExchangeInterface(market_type='future')
    
    bots_to_audit = []
    if args.bot_id:
        bots_to_audit.append(args.bot_id)
    else:
        # Get all active bots
        conn = get_connection()
        cursor = conn.cursor()
        # Only check bots that think they are in trade? Or all?
        # Better to check all "In Trade" bots first.
        cursor.execute("SELECT id FROM bots WHERE is_active=1")
        bots_to_audit = [r[0] for r in cursor.fetchall()]
        conn.close()
        
    print(f"Auditing {len(bots_to_audit)} bots...")
    
    for bib in bots_to_audit:
        try:
            audit_bot(bib, exchange, args)
        except Exception as e:
            print(f"❌ Error auditing Bot {bib}: {e}")
            import traceback
            traceback.print_exc()
            
    print("\nAudit Complete.")

if __name__ == "__main__":
    main()
