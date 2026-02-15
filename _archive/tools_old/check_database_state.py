#!/usr/bin/env python3
"""
Check sync status between bot database and exchange
"""

import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "crypto_bot.db"

def main():
    print("="*80)
    print("DATABASE STATE CHECK - Positions and Orders")
    print("="*80)
    print()
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check active positions
    print("[1] ACTIVE POSITIONS IN DATABASE")
    print("-"*80)
    cursor.execute("SELECT COUNT(*) FROM active_positions")
    pos_count = cursor.fetchone()[0]
    print(f"Total Positions: {pos_count}")
    
    if pos_count > 0:
        cursor.execute("""
            SELECT pair, side, size, entry_price, owner_bot_id, last_updated
            FROM active_positions
        """)
        positions = cursor.fetchall()
        print()
        for pair, side, size, entry, bot_id, updated in positions:
            print(f"  Pair: {pair}")
            print(f"  Side: {side}")
            print(f"  Size: {size}")
            print(f"  Entry Price: ${entry:.2f}")
            print(f"  Owner Bot ID: {bot_id}")
            print(f"  Last Updated: {updated}")
            print()
    else:
        print("  ✓ No active positions in database")
    
    print()
    
    # Check open orders
    print("[2] OPEN ORDERS IN DATABASE")
    print("-"*80)
    cursor.execute("""
        SELECT COUNT(*) FROM bot_orders 
        WHERE status IN ('open', 'pending')
    """)
    order_count = cursor.fetchone()[0]
    print(f"Total Open Orders: {order_count}")
    
    if order_count > 0:
        cursor.execute("""
            SELECT bot_id, order_type, price, amount, status, order_id, created_at
            FROM bot_orders 
            WHERE status IN ('open', 'pending')
            LIMIT 50
        """)
        orders = cursor.fetchall()
        print()
        for bot_id, otype, price, amount, status, oid, created in orders:
            print(f"  Bot ID: {bot_id}")
            print(f"  Type: {otype}")
            print(f"  Price: ${price:.2f}")
            print(f"  Amount: {amount:.6f}")
            print(f"  Status: {status}")
            print(f"  Order ID: {oid}")
            print(f"  Created: {created}")
            print()
    else:
        print("  ✓ No open orders in database")
    
    print()
    
    # Check all orders by status
    print("[3] ORDER STATUS BREAKDOWN")
    print("-"*80)
    cursor.execute("""
        SELECT status, COUNT(*) 
        FROM bot_orders 
        GROUP BY status
    """)
    status_counts = cursor.fetchall()
    for status, count in status_counts:
        print(f"  {status:15s}: {count}")
    
    print()
    
    # Check bot ownership state
    print("[4] BOT OWNERSHIP STATE")
    print("-"*80)
    cursor.execute("SELECT COUNT(*) FROM bot_ownership_state")
    ownership_count = cursor.fetchone()[0]
    print(f"Total Ownership Records: {ownership_count}")
    
    if ownership_count > 0:
        cursor.execute("""
            SELECT bot_id, pair, position_size, avg_entry_price
            FROM bot_ownership_state
            WHERE position_size > 0
        """)
        ownerships = cursor.fetchall()
        if ownerships:
            print()
            for bot_id, pair, size, avg_price in ownerships:
                print(f"  Bot {bot_id}: {pair} - Size: {size:.6f} @ ${avg_price:.2f}")
        else:
            print("  ✓ No active bot ownership positions")
    
    print()
    
    # Check trade history
    print("[5] RECENT TRADE HISTORY")
    print("-"*80)
    cursor.execute("SELECT COUNT(*) FROM trades")
    trade_count = cursor.fetchone()[0]
    print(f"Total Trades: {trade_count}")
    
    if trade_count > 0:
        cursor.execute("""
            SELECT bot_id, current_step, total_invested, avg_entry_price, last_exit_time
            FROM trades
            ORDER BY last_exit_time DESC
            LIMIT 10
        """)
        trades = cursor.fetchall()
        print()
        print("  Recent 10 Trades:")
        for bot_id, step, invested, avg_price, exit_time in trades:
            print(f"    Bot {bot_id}: Step {step} - Invested: ${invested:.2f} @ ${avg_price:.2f} (exit: {exit_time})")
    
    print()
    
    conn.close()
    
    print("="*80)
    print("DATABASE CHECK COMPLETE")
    print("="*80)

if __name__ == "__main__":
    import sys
    with open("db_report.txt", "w", encoding="utf-8") as f:
        sys.stdout = f
        try:
            main()
        finally:
            sys.stdout = sys.__stdout__
