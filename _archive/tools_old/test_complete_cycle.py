#!/usr/bin/env python3
"""
FINAL END-TO-END TEST: Complete Trading Cycle Verification
Simulates: Entry → Add orders → Close position → Verify cleanup
"""

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from engine.database import get_connection, reset_bot_after_tp
import time

def test_complete_cycle():
    """Simulate a complete trading cycle and verify cleanup"""
    
    print("="*70)
    print("END-TO-END TEST: Complete Trading Cycle")
    print("="*70)
    print()
    
    bot_id = 43
    conn = get_connection()
    cur = conn.cursor()
    
    # PHASE 1: Setup - Bot enters a position
    print("PHASE 1: Bot enters position")
    print("-"*70)
    
    cur.execute("""
        UPDATE trades 
        SET total_invested = 5000.0,
            current_step = 2,
            avg_entry_price = 60000.0,
            target_tp_price = 62000.0
        WHERE bot_id = ?
    """, (bot_id,))
    
    # Add orders: 1 entry, 1 grid, 1 TP (typical setup)
    cur.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at)
        VALUES 
            (?, 0, 'entry', 'CYCLE_ENTRY', 60000.0, 0.083, 'open', ?),
            (?, 1, 'grid', 'CYCLE_GRID', 59000.0, 0.042, 'open', ?),
            (?, 2, 'tp', 'CYCLE_TP', 62000.0, 0.125, 'open', ?)
    """, (bot_id, int(time.time()), bot_id, int(time.time()), bot_id, int(time.time())))
    conn.commit()
    
    # Verify position state
    cur.execute('SELECT total_invested, current_step FROM trades WHERE bot_id = ?', (bot_id,))
    invested, step = cur.fetchone()
    cur.execute('SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = "open"', (bot_id,))
    orders_count = cur.fetchone()[0]
    
    print(f"✓ Position created: ${invested} invested, step {step}")
    print(f"✓ Orders created: {orders_count} open orders")
    print()
    
    # PHASE 2: Simulate TP hit - Position closes
    print("PHASE 2: TP Hit - Position closes")
    print("-"*70)
    
    reset_bot_after_tp(bot_id, exit_price=62100.0, direction='LONG', action_label='TP_HIT')
    
    # PHASE 3: Verify cleanup
    print()
    print("PHASE 3: Verification")
    print("-"*70)
    
    cur.execute('SELECT total_invested, current_step FROM trades WHERE bot_id = ?', (bot_id,))
    invested, step = cur.fetchone()
    
    cur.execute('SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = "open"', (bot_id,))
    open_count = cur.fetchone()[0]
    
    cur.execute('SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = "auto_closed" AND order_id LIKE "CYCLE_%"', (bot_id,))
    closed_count = cur.fetchone()[0]
    
    print(f"Position state:")
    print(f"  Invested: ${invested}")
    print(f"  Step: {step}")
    print(f"Order state:")
    print(f"  Open: {open_count}")
    print(f"  Auto-closed: {closed_count}")
    print()
    
    # Success criteria
    success = (
        invested == 0 and
        step == 0 and
        open_count == 0 and
        closed_count == 3
    )
    
    if success:
        print("="*70)
        print("✅ SUCCESS: Complete trading cycle cleanup works!")
        print("="*70)
        print()
        print("What happened:")
        print("  1. Bot entered position with $5000 invested")
        print("  2. Created 3 orders (entry, grid, TP)")
        print("  3. TP hit at $62,100")
        print("  4. Position reset to $0")
        print("  5. All 3 orders automatically cleaned up")
        print()
        print("This proves the fix works in a REAL trading scenario!")
        return 0
    else:
        print("="*70)
        print("❌ FAILED: Cleanup did not work correctly")
        print("="*70)
        print(f"Expected: invested=0, step=0, open=0, auto_closed=3")
        print(f"Got: invested={invested}, step={step}, open={open_count}, auto_closed={closed_count}")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(test_complete_cycle())
    except Exception as e:
        print(f"\n❌ TEST CRASHED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
