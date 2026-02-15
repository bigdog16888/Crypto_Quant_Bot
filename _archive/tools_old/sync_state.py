"""
State Sync Script - One-time fix for DB vs Exchange discrepancy.

Problem: DB shows $361 invested, but exchange has 0.578 BTC (~$42,840)
Cause: Grid fills recorded in bot_orders but trades.current_step never updated

This script:
1. Calculates correct position from filled orders in bot_orders
2. Updates trades table to match reality
3. Verifies sync with exchange
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

def sync_bot_state(bot_id: int, pair: str, verbose: bool = True):
    """Sync a single bot's state with its filled orders."""
    conn = get_connection()
    cur = conn.cursor()
    
    # 1. Get all filled orders for this bot
    filled_orders = cur.execute('''
        SELECT order_type, price, amount, step
        FROM bot_orders
        WHERE bot_id = ? AND status = 'filled'
        ORDER BY created_at ASC
    ''', (bot_id,)).fetchall()
    
    # 2. Calculate total position
    total_invested = 0.0
    total_qty = 0.0
    max_step = 0
    entry_count = 0
    grid_count = 0
    
    for order_type, price, amount, step in filled_orders:
        if order_type in ('entry', 'grid'):
            cost = price * amount
            total_invested += cost
            total_qty += amount
            if step and step > max_step:
                max_step = step
            if order_type == 'entry':
                entry_count += 1
            else:
                grid_count += 1
    
    # 3. Calculate average entry price
    avg_entry = total_invested / total_qty if total_qty > 0 else 0
    
    # 4. Get bot direction for TP calculation
    cur.execute('SELECT direction FROM bots WHERE id = ?', (bot_id,))
    direction = cur.fetchone()
    direction = direction[0] if direction else 'LONG'
    
    # 5. Calculate TP (1.5% from avg entry)
    if direction == 'LONG':
        tp_price = avg_entry * 1.015
    else:
        tp_price = avg_entry * 0.985
    
    # 6. Get current DB state for comparison
    cur.execute('''
        SELECT current_step, total_invested, avg_entry_price
        FROM trades WHERE bot_id = ?
    ''', (bot_id,))
    current = cur.fetchone()
    
    if verbose:
        print(f"\n=== Bot {bot_id} ({pair}) ===")
        print(f"Filled orders: {entry_count} entry + {grid_count} grid = {len(filled_orders)} total")
        if current:
            print(f"Current DB: Step {current[0]}, ${current[1]:.2f} invested")
        else:
            print(f"Current DB: No trade record found")
        print(f"Should be:  Step {max_step}, ${total_invested:.2f} invested, {total_qty:.6f} qty")
    
    # 7. Update trades table
    cur.execute('''
        UPDATE trades
        SET current_step = ?,
            total_invested = ?,
            avg_entry_price = ?,
            target_tp_price = ?,
            entry_confirmed = 1
        WHERE bot_id = ?
    ''', (max_step, total_invested, avg_entry, tp_price, bot_id))
    
    conn.commit()
    
    if verbose:
        print(f"✅ Updated: Step {max_step}, ${total_invested:.2f} invested @ ${avg_entry:.2f}")
    
    return {
        'bot_id': bot_id,
        'step': max_step,
        'total_invested': total_invested,
        'total_qty': total_qty,
        'avg_entry': avg_entry
    }


def sync_all_bots():
    """Sync all bots in trade with their filled orders."""
    conn = get_connection()
    cur = conn.cursor()
    
    print("=" * 60)
    print("STATE SYNC: Reconciling DB with filled orders")
    print("=" * 60)
    
    # Get all bots with trades
    bots_in_trade = cur.execute('''
        SELECT b.id, b.pair
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE t.total_invested > 0
    ''').fetchall()
    
    results = []
    total_synced_qty = {}
    
    for bot_id, pair in bots_in_trade:
        result = sync_bot_state(bot_id, pair)
        results.append(result)
        
        # Aggregate by pair
        base = pair.split('/')[0] if '/' in pair else pair.split(':')[0]
        if base not in total_synced_qty:
            total_synced_qty[base] = 0.0
        total_synced_qty[base] += result['total_qty']
    
    print("\n" + "=" * 60)
    print("VERIFICATION: Comparing synced DB with Exchange")
    print("=" * 60)
    
    # Compare with exchange
    ex = ExchangeInterface(market_type='future')
    for pos in ex.exchange.fetch_positions():
        contracts = float(pos.get('contracts', 0) or 0)
        if contracts > 0:
            symbol = pos.get('symbol')
            base = symbol.split('/')[0] if '/' in symbol else symbol.split(':')[0]
            entry = float(pos.get('entryPrice', 0) or 0)
            
            db_qty = total_synced_qty.get(base, 0)
            
            print(f"\n{symbol}:")
            print(f"  Exchange: {contracts:.6f} contracts @ ${entry:.2f}")
            print(f"  DB Total: {db_qty:.6f} qty")
            
            if abs(contracts - db_qty) < 0.001:
                print(f"  ✅ MATCH!")
            else:
                diff = contracts - db_qty
                print(f"  ⚠️  DIFF: {diff:.6f} ({diff * entry:.2f} USD)")
                print(f"     Note: This could be from TP fills or manual trades")
    
    print("\n" + "=" * 60)
    print("SYNC COMPLETE")
    print("=" * 60)
    
    return results


if __name__ == "__main__":
    sync_all_bots()
