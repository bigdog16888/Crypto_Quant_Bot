#!/usr/bin/env python3
"""
Phase 2 — 4-bot cycle_id migration (DRY-RUN)

Ground truth: The TRUE cycle_id for a bot is the MAX cycle_id in bot_orders
that has any filled_amount > 0. This is the cycle the bot was actually trading in.

The trades row should be corrected to:
- cycle_id = max_fill_cycle_id
- open_qty, total_invested, avg_entry_price = recomputed from ALL fills in that cycle
- cache columns reset (current_step=0, entry_confirmed=0, cycle_phase='IDLE', etc.)

Usage (dry-run):
    python scripts/migrate_4bot_cycle_correction.py

Usage (apply - requires explicit approval):
    python scripts/migrate_4bot_cycle_correction.py --apply

Affected bots: 10016, 10007, 100314, 100317
"""

import sys
import os
import argparse
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine import database

# Statuses that count as confirmed fills (matching recompute_invested_from_orders)
FILLED_STATUSES = ('filled', 'partially_filled', 'closed', 'auto_closed', 'hedge_exited', 'reset_cleared')

ENTRY_TYPES = ('entry', 'grid', 'adoption', 'adoption_add', 'carry')
EXIT_TYPES  = ('tp', 'close', 'dust_close', 'sl', 'adoption_reduce', 'flatten_close')


def compute_state_from_cycle_fills(conn, bot_id, cycle_id):
    """
    Recompute open_qty, total_invested, avg_entry_price from ALL fills in a given cycle.
    Uses FIFO matching (same as recompute_invested_from_orders).
    """
    rows = conn.execute('''
        SELECT order_type, filled_amount, price
        FROM bot_orders
        WHERE bot_id = ? 
          AND cycle_id = ?
          AND filled_amount > 0
          AND status IN ('filled','partially_filled','closed','auto_closed','hedge_exited','reset_cleared')
        ORDER BY created_at, id
    ''', (bot_id, cycle_id)).fetchall()
    
    if not rows:
        return 0.0, 0.0, 0.0
    
    entries = []
    total_exit_qty = 0.0
    
    for otype, qty, price in rows:
        qty = float(qty)
        price = float(price)
        if otype in ENTRY_TYPES:
            entries.append({'qty': qty, 'price': price})
        elif otype in EXIT_TYPES:
            total_exit_qty += qty
    
    if not entries:
        return 0.0, 0.0, 0.0
    
    # FIFO: consume earliest entries first
    remaining_exit = total_exit_qty
    for entry in entries:
        if remaining_exit <= 1e-8:
            break
        if entry['qty'] <= remaining_exit + 1e-8:
            remaining_exit -= entry['qty']
            entry['qty'] = 0.0
        else:
            entry['qty'] -= remaining_exit
            remaining_exit = 0.0
    
    # Remaining entries = open position
    open_qty = sum(e['qty'] for e in entries)
    if open_qty <= 1e-8:
        return 0.0, 0.0, 0.0
    
    total_cost = sum(e['qty'] * e['price'] for e in entries)
    avg_price = total_cost / open_qty
    total_invested = open_qty * avg_price
    
    return open_qty, total_invested, avg_price


AFFECTED_BOTS = {
    10016: {'name': 'long btc price', 'pair': 'BTC/USDC:USDC', 'direction': 'LONG'},
    10007: {'name': 'bnb short', 'pair': 'BNB/USDC:USDC', 'direction': 'SHORT'},
    100314: {'name': 'bnb short_hedge', 'pair': 'BNB/USDC:USDC', 'direction': 'LONG'},
    100317: {'name': 'long btc price_hedge', 'pair': 'BTC/USDC:USDC', 'direction': 'SHORT'},
}


def run_migration(db_path, apply=False):
    database.DB_PATH = db_path
    database.close_connection()
    conn = database.get_connection()
    
    print("=" * 80)
    print("PHASE 2 — 4-BOT CYCLE_ID MIGRATION")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Database: {db_path}")
    print("=" * 80)
    
    all_updates = []
    
    for bot_id, info in AFFECTED_BOTS.items():
        print(f"\n{'='*60}")
        print(f"Bot {bot_id} ({info['name']}, {info['direction']})")
        print(f"{'='*60}")
        
        # Current state
        t_row = conn.execute('SELECT cycle_id, open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id=?', (bot_id,)).fetchone()
        current_cycle = t_row[0] if t_row else 1
        current_open_qty = t_row[1] if t_row else 0.0
        current_invested = t_row[2] if t_row else 0.0
        current_avg = t_row[3] if t_row else 0.0
        
        print(f"  Current trades: cycle_id={current_cycle}, open_qty={current_open_qty:.8f}, total_invested={current_invested:.4f}, avg_entry={current_avg:.4f}")
        
        # Ground truth: TRUE cycle_id = max cycle_id with fills in bot_orders
        true_cycle_id = conn.execute('''
            SELECT MAX(cycle_id) FROM bot_orders 
            WHERE bot_id = ? AND filled_amount > 0
              AND status IN ('filled','partially_filled','closed','auto_closed','hedge_exited','reset_cleared')
        ''', (bot_id,)).fetchone()[0]
        
        if true_cycle_id is None:
            print(f"  No filled orders found - skipping")
            continue
        
        # Compute true state from fills in that cycle
        true_open_qty, true_invested, true_avg = compute_state_from_cycle_fills(conn, bot_id, true_cycle_id)
        
        print(f"  True cycle_id (max fill cycle): {true_cycle_id}")
        print(f"  True open_qty: {true_open_qty:.8f}")
        print(f"  True total_invested: {true_invested:.4f}")
        print(f"  True avg_entry_price: {true_avg:.4f}")
        
        # Determine what needs updating
        updates = []
        if current_cycle != true_cycle_id:
            updates.append(f"cycle_id: {current_cycle} → {true_cycle_id}")
        if abs(current_open_qty - true_open_qty) > 1e-8:
            updates.append(f"open_qty: {current_open_qty:.8f} → {true_open_qty:.8f}")
        if abs(current_invested - true_invested) > 0.01:
            updates.append(f"total_invested: {current_invested:.4f} → {true_invested:.4f}")
        if abs(current_avg - true_avg) > 0.01:
            updates.append(f"avg_entry_price: {current_avg:.4f} → {true_avg:.4f}")
        
        if updates:
            print(f"  CHANGES NEEDED:")
            for u in updates:
                print(f"    - {u}")
            
            # Build SQL
            set_clauses = []
            params = []
            if current_cycle != true_cycle_id:
                set_clauses.append("cycle_id = ?")
                params.append(true_cycle_id)
            if abs(current_open_qty - true_open_qty) > 1e-8:
                set_clauses.append("open_qty = ?")
                params.append(true_open_qty)
            if abs(current_invested - true_invested) > 0.01:
                set_clauses.append("total_invested = ?")
                params.append(true_invested)
            if abs(current_avg - true_avg) > 0.01:
                set_clauses.append("avg_entry_price = ?")
                params.append(true_avg)
            
            # Always reset cache columns on cycle correction
            set_clauses.extend([
                "current_step = 0",
                "entry_confirmed = 0",
                "cycle_phase = 'IDLE'",
                "entry_order_id = NULL",
                "tp_order_id = NULL",
                "wipe_wall_ts = ?",
                "cycle_start_time = ?"
            ])
            now_ts = int(time.time())
            params.extend([now_ts, now_ts, bot_id])
            
            sql = f"UPDATE trades SET {', '.join(set_clauses)} WHERE bot_id = ?"
            print(f"  SQL: {sql}")
            print(f"  PARAMS: {params}")
            
            all_updates.append({
                'bot_id': bot_id,
                'sql': sql,
                'params': params
            })
        else:
            print(f"  No changes needed")
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(all_updates)} bots need updates")
    print(f"{'='*60}")
    
    if apply and all_updates:
        print("APPLYING changes...")
        for upd in all_updates:
            conn.execute(upd['sql'], upd['params'])
        conn.commit()
        print("COMMITTED.")
    elif all_updates:
        print("DRY-RUN complete. No writes executed.")
    
    conn.close()
    return all_updates


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Actually write changes (default: dry-run)')
    args = parser.parse_args()
    
    db_path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
    run_migration(db_path, apply=args.apply)