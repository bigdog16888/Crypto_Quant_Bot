#!/usr/bin/env python3
"""
Simple verification: How many bots in trade, open orders, and positions on exchange
This script uses the ACTUAL database schema and works reliably.
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from engine.database import get_connection
from config.settings import config

def main():
    print("="*60)
    print("QUICK STATUS CHECK")
    print("="*60)
    print()
    
    # ========================================
    # 1. DATABASE CHECKS
    # ========================================
    print("[DATABASE]")
    print("-"*60)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # How many bots are in a trade (total_invested > 0)
    cursor.execute("""
        SELECT COUNT(*) 
        FROM trades 
        WHERE total_invested > 0
    """)
    bots_in_trade = cursor.fetchone()[0]
    print(f"Bots in trade (invested > 0): {bots_in_trade}")
    
    if bots_in_trade > 0:
        cursor.execute("""
            SELECT b.id, b.name, b.pair, t.total_invested, t.current_step
            FROM bots b
            JOIN trades t ON b.id = t.bot_id
            WHERE t.total_invested > 0
            ORDER BY b.id
        """)
        for bot_id, name, pair, invested, step in cursor.fetchall():
            print(f"  Bot {bot_id:3d} ({name:20s}): {pair:12s} Step {step} - ${invested:,.2f}")
    
    # How many open orders in database
    cursor.execute("""
        SELECT COUNT(*) 
        FROM bot_orders 
        WHERE status = 'open'
    """)
    open_orders_db = cursor.fetchone()[0]
    print(f"\nOpen orders in DB: {open_orders_db}")
    
    if open_orders_db > 0:
        cursor.execute("""
            SELECT bot_id, COUNT(*), order_type
            FROM bot_orders 
            WHERE status = 'open'
            GROUP BY bot_id, order_type
            ORDER BY bot_id
        """)
        for bot_id, count, order_type in cursor.fetchall():
            print(f"  Bot {bot_id:3d}: {count:3d} {order_type} orders")
    
    print()
    
    # ========================================
    # 2. EXCHANGE CHECKS
    # ========================================
    print("[EXCHANGE]")
    print("-"*60)
    
    open_positions = []
    
    try:
        from engine.exchange_interface import ExchangeInterface
        # Initialize with future market type to see BTC/USDC positions
        exchange = ExchangeInterface(market_type='future')
        
        # Get all positions from exchange
        try:
            positions = exchange.fetch_positions()
            # Debug: Print raw positions if needed
            # print(f"DEBUG: Found {len(positions)} raw positions")
            
            open_positions = []
            for p in positions:
                contracts = float(p.get('contracts', 0))
                # Some exchanges return string '0' or None
                if contracts != 0.0:
                    open_positions.append(p)
            
            print(f"Open positions on exchange: {len(open_positions)}")
            
            if open_positions:
                for pos in open_positions:
                    symbol = pos.get('symbol', 'N/A')
                    side = pos.get('side', 'N/A')
                    contracts = float(pos.get('contracts', 0))
                    entry = float(pos.get('entryPrice', 0))
                    notional = float(pos.get('notional', 0))
                    pnl = float(pos.get('unrealizedPnl', 0))
                    
                    print(f"  {symbol:12s} {side:5s} {contracts:10.6f} @ ${entry:10.2f}")
                    print(f"    Notional: ${notional:,.2f}, PnL: ${pnl:+,.2f}")
        except Exception as e:
            print(f"Error fetching positions: {e}")
            open_positions = []
        
        # Get open orders from exchange (all symbols)
        # Note: fetch_open_orders() without symbol gets all
        try:
            orders = exchange.fetch_open_orders()
            print(f"\nOpen orders on exchange: {len(orders)}")
            
            if orders:
                order_by_symbol = {}
                for order in orders:
                    symbol = order.get('symbol', 'N/A')
                    if symbol not in order_by_symbol:
                        order_by_symbol[symbol] = 0
                    order_by_symbol[symbol] += 1
                
                for symbol, count in sorted(order_by_symbol.items()):
                    print(f"  {symbol:12s}: {count} orders")
        except Exception as e:
            print(f"\nCannot fetch all orders: {e}")
            print("(This is normal for some exchanges - requires per-symbol fetch)")
        
    except Exception as e:
        print(f"❌ Cannot connect to exchange: {e}")
        print("\nSkipping exchange checks...")
    
    print()
    
    # ========================================
    # 3. SYNC SUMMARY
    # ========================================
    print("[SYNC SUMMARY]")
    print("="*60)
    
    if bots_in_trade == 0 and len(open_positions) == 0:
        print("✅ CLEAN STATE: No bots in trade, no exchange positions")
    elif bots_in_trade > 0 and len(open_positions) > 0:
        print(f"⚠️  {bots_in_trade} bots in trade, {len(open_positions)} exchange positions")
        print("   Manual verification recommended")
    elif bots_in_trade > 0 and len(open_positions) == 0:
        print(f"❌ MISMATCH: {bots_in_trade} bots show invested, but NO exchange positions!")
        print("   → Positions may have been closed externally")
        print("   → Run reconciliation to sync database")
    elif bots_in_trade == 0 and len(open_positions) > 0:
        print(f"❌ MISMATCH: {len(open_positions)} exchange positions, but NO bots show invested!")
        print("   → These may be manual trades or external positions")
        print("   → Bots will not manage these positions")
    
    if open_orders_db > 0:
        print(f"\n⚠️  {open_orders_db} orders in database")
        print("   → Verify these exist on exchange")
    
    print()
    print("="*60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
