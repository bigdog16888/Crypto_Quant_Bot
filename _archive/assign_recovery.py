#!/usr/bin/env python3
"""
Assign orphan BTC/USDC position to bot 41
"""
import sys
import time
import os

# Add the engine path to import database module
engine_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, engine_path)

from database import get_connection, log_trade, get_bot_status

# Bot and position details
BOT_ID = 41
SYMBOL = "BTC/USDC:USDC"
SIDE = "LONG"
SIZE = 0.008  # contracts
ENTRY_PRICE = 85200.834
NOTIONAL = 660.00

def assign_orphan_position():
    """Assign the orphan position to bot 41"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Check if bot 41 exists and get current status
        print(f"Checking bot {BOT_ID} status...")
        current_status = get_bot_status(BOT_ID)
        if current_status:
            print(f"  Bot name: {current_status[0]}")
            print(f"  Current pair: {current_status[1]}")
            print(f"  Current step: {current_status[2]}")
            print(f"  Current invested: ${current_status[3]:.2f}")
            print(f"  Current avg entry: ${current_status[4]:.4f}")
        else:
            print(f"  ERROR: Bot {BOT_ID} not found!")
            return False
        
        # Check if there's already an active position
        cursor.execute('SELECT current_step, total_invested FROM trades WHERE bot_id = ?', (BOT_ID,))
        result = cursor.fetchone()
        if result and result[0] > 0:
            print(f"  WARNING: Bot {BOT_ID} already has an active position (step={result[0]})")
            response = input("  Continue anyway? This will OVERWRITE existing position. (yes/no): ")
            if response.lower() != 'yes':
                print("  Aborted.")
                return False
        
        current_time = int(time.time())
        
        # Update trades table with orphan position data
        print(f"\nUpdating trades table for bot {BOT_ID}...")
        cursor.execute('''
            UPDATE trades 
            SET current_step = 1,
                total_invested = ?,
                avg_entry_price = ?,
                entry_confirmed = 1,
                basket_start_time = ?,
                target_tp_price = 0,
                last_exit_price = 0,
                last_exit_time = 0
            WHERE bot_id = ?
        ''', (NOTIONAL, ENTRY_PRICE, current_time, BOT_ID))
        
        if cursor.rowcount == 0:
            print(f"  ERROR: No rows updated - bot {BOT_ID} may not exist in trades table")
            return False
        
        print(f"  Updated: step=1, invested=${NOTIONAL:.2f}, entry=${ENTRY_PRICE:.4f}")
        
        # Log RECOVERY trade to trade_history
        print(f"\nLogging RECOVERY trade to trade_history...")
        log_trade(
            bot_id=BOT_ID,
            action='RECOVERY',
            symbol=SYMBOL,
            price=ENTRY_PRICE,
            amount=SIZE,
            cost_usdc=NOTIONAL,
            order_id='ORPHAN_RECOVERY',
            step=1,
            pnl=0.0,
            notes=f'Assigned orphan position: {SIDE} {SIZE} contracts at ${ENTRY_PRICE:.4f}, notional=${NOTIONAL:.2f}'
        )
        print(f"  Logged RECOVERY trade")
        
        # Commit all changes
        conn.commit()
        print(f"\nChanges committed successfully")
        
        # Verify the update
        print(f"\nVerifying update for bot {BOT_ID}...")
        verify_status = get_bot_status(BOT_ID)
        if verify_status:
            print(f"  Verification successful:")
            print(f"    Name: {verify_status[0]}")
            print(f"    Pair: {verify_status[1]}")
            print(f"    Current step: {verify_status[2]}")
            print(f"    Total invested: ${verify_status[3]:.2f}")
            print(f"    Avg entry price: ${verify_status[4]:.4f}")
            
            # Verify the values match what we set
            if verify_status[2] == 1 and abs(verify_status[3] - NOTIONAL) < 0.01 and abs(verify_status[4] - ENTRY_PRICE) < 0.0001:
                print(f"\nSUCCESS: All values match expected")
                return True
            else:
                print(f"\nWARNING: Values don't match expected!")
                return False
        else:
            print(f"  ERROR: Could not verify - bot status not found")
            return False
            
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
        return False

if __name__ == "__main__":
    print("="*60)
    print("ASSIGN ORPHAN BTC/USDC POSITION TO BOT 41")
    print("="*60)
    print(f"\nPosition Details:")
    print(f"  Symbol: {SYMBOL}")
    print(f"  Side: {SIDE}")
    print(f"  Size: {SIZE} contracts")
    print(f"  Entry Price: ${ENTRY_PRICE:.4f}")
    print(f"  Notional: ${NOTIONAL:.2f}")
    print(f"\nTarget Bot: {BOT_ID}")
    print()
    
    success = assign_orphan_position()
    
    if success:
        print("\n" + "="*60)
        print("RECOVERY COMPLETE")
        print("="*60)
        sys.exit(0)
    else:
        print("\n" + "="*60)
        print("RECOVERY FAILED")
        print("="*60)
        sys.exit(1)
