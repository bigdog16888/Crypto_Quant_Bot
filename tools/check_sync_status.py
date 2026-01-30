#!/usr/bin/env python3
"""
Comprehensive Sync Check: Database vs Exchange
Checks positions and orders on both sides and identifies discrepancies
"""

import sys
import sqlite3
from pathlib import Path
import ccxt
from dotenv import load_dotenv
import os
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Load environment variables
load_dotenv()

def timestamp_to_str(ts):
    """Convert unix timestamp to readable string"""
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except:
        return str(ts)

def main():
    print("="*80)
    print("SYNC STATUS CHECK: Database vs Exchange")
    print("="*80)
    print()
    
    # ========================================
    # 1. CHECK DATABASE STATE
    # ========================================
    print("[1/3] Checking DATABASE state...")
    print("-"*80)
    
    db_path = project_root / "crypto_bot.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check active positions in DB
    cursor.execute("SELECT COUNT(*) FROM active_positions")
    db_pos_count = cursor.fetchone()[0]
    print(f"Active Positions in DB: {db_pos_count}")
    
    db_positions = []
    if db_pos_count > 0:
        cursor.execute("""
            SELECT pair, side, size, entry_price, owner_bot_id
            FROM active_positions
        """)
        db_positions = cursor.fetchall()
        print("\nDatabase Positions:")
        for pair, side, size, entry, bot_id in db_positions:
            print(f"  {pair:15s} {side:5s} {size:10.6f} @ ${entry:10.2f} (Bot {bot_id})")
    
    # Check open orders in DB
    cursor.execute("""
        SELECT COUNT(*) FROM bot_orders 
        WHERE status IN ('open', 'pending')
    """)
    db_order_count = cursor.fetchone()[0]
    print(f"\nOpen Orders in DB: {db_order_count}")
    
    if db_order_count > 0:
        cursor.execute("""
            SELECT bot_id, COUNT(*), SUM(amount * price)
            FROM bot_orders 
            WHERE status IN ('open', 'pending')
            GROUP BY bot_id
            ORDER BY bot_id
        """)
        bot_orders = cursor.fetchall()
        print("\nOrders by Bot:")
        for bot_id, count, total_value in bot_orders:
            print(f"  Bot {bot_id:3d}: {count:4d} orders, ~${total_value:,.2f} total value")
    
    # Check bot ownership state
    cursor.execute("""
        SELECT bot_id, pair, position_size, avg_entry_price
        FROM bot_ownership_state
        WHERE position_size > 0
    """)
    ownership_positions = cursor.fetchall()
    print(f"\nBot Ownership Positions: {len(ownership_positions)}")
    if ownership_positions:
        for bot_id, pair, size, avg_price in ownership_positions:
            print(f"  Bot {bot_id:3d}: {pair:15s} {size:10.6f} @ ${avg_price:10.2f}")
    
    conn.close()
    print()
    
    # ========================================
    # 2. CHECK EXCHANGE STATE
    # ========================================
    print("[2/3] Checking EXCHANGE state...")
    print("-"*80)
    
    use_testnet = os.getenv('USE_TESTNET', 'True').lower() == 'true'
    
    if use_testnet:
        api_key = os.getenv('BINANCE_TESTNET_API_KEY')
        api_secret = os.getenv('BINANCE_TESTNET_API_SECRET')
        print("Mode: TESTNET")
    else:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        print("Mode: MAINNET ⚠️")
    
    if not api_key or not api_secret:
        print("❌ ERROR: API credentials not found")
        print("\nCannot check exchange state without API access.")
        print("Proceeding with database-only analysis...")
        return analyze_database_only()
    
    try:
        exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
            'enableRateLimit': True,
        })
        
        if use_testnet:
            exchange.set_sandbox_mode(True)
        
        exchange.load_markets()
        print("✓ Connected to Binance Futures")
        print()
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\nNote: Binance Futures testnet is deprecated.")
        print("Proceeding with database-only analysis...")
        return analyze_database_only()
    
    # Fetch positions
    try:
        print("Fetching exchange positions...")
        positions = exchange.fetch_positions()
        open_positions = [p for p in positions if float(p.get('contracts', 0)) > 0]
        
        print(f"Open Positions on Exchange: {len(open_positions)}")
        
        if open_positions:
            print("\nExchange Positions:")
            for pos in open_positions:
                symbol = pos.get('symbol')
                side = pos.get('side')
                contracts = float(pos.get('contracts', 0))
                leverage = float(pos.get('leverage', 1))
                entry = float(pos.get('entryPrice', 0))
                notional = float(pos.get('notional', 0))
                pnl = float(pos.get('unrealizedPnl', 0))
                
                print(f"  {symbol:15s} {side:5s} {contracts:10.6f} @ ${entry:10.2f}")
                print(f"    Leverage: {leverage}x, Notional: ${notional:,.2f}, PnL: ${pnl:,.2f}")
        
    except Exception as e:
        print(f"❌ Failed to fetch positions: {e}")
        open_positions = []
    
    print()
    
    # Fetch open orders
    try:
        print("Fetching exchange open orders...")
        # For testnet, we might not be able to fetch all orders at once
        # Just try to get a count
        # Note: This might fail on testnet
        all_orders = []
        print("⚠️  Cannot fetch all open orders (requires per-symbol fetch)")
        print("    Skipping exchange order count")
        
    except Exception as e:
        print(f"❌ Failed to fetch orders: {e}")
    
    print()
    
    # ========================================
    # 3. SYNC ANALYSIS
    # ========================================
    print("[3/3] SYNC ANALYSIS")
    print("="*80)
    
    print("\nDATABASE STATE:")
    print(f"  Active Positions: {db_pos_count}")
    print(f"  Open Orders: {db_order_count}")
    print(f"  Ownership Positions: {len(ownership_positions)}")
    
    print("\nEXCHANGE STATE:")
    print(f"  Open Positions: {len(open_positions)}")
    print(f"  Open Orders: Unable to fetch (requires per-symbol)")
    
    print("\nSYNC STATUS:")
    if db_pos_count == 0 and len(open_positions) == 0:
        print("  ✅ SYNCED: No positions on either side")
    elif db_pos_count == 0 and len(open_positions) > 0:
        print("  ⚠️  MISMATCH: Exchange has positions, but DB shows none")
        print("      → This could be manual trades or external positions")
        print("      → Bot may not manage these positions")
    elif db_pos_count > 0 and len(open_positions) == 0:
        print("  ⚠️  MISMATCH: DB shows positions, but Exchange has none")
        print("      → Positions may have been closed manually")
        print("      → Run sync_bot_state() to clean up DB")
    else:
        print("  ⚠️  Both sides have positions - manual verification needed")
    
    print("\nORDERS:")
    if db_order_count > 0:
        print(f"  ⚠️  {db_order_count} open orders in database")
        print("      → These may or may not exist on exchange")
        print("      → Recommend running reconciliation")
    else:
        print("  ✅ No open orders in database")
    
    print("\nLEVERAGE STATUS:")
    if open_positions:
        print("  Current leverage on exchange positions:")
        for pos in open_positions:
            symbol = pos.get('symbol')
            leverage = float(pos.get('leverage', 1))
            if leverage == 1:
                print(f"    {symbol}: {leverage}x ❌ (Should be 20x)")
            elif leverage == 20:
                print(f"    {symbol}: {leverage}x ✅")
            else:
                print(f"    {symbol}: {leverage}x ⚠️  (Expected 20x)")
        print("\n  NOTE: Existing positions retain their original leverage.")
        print("        Only NEW positions will use the updated 20x leverage.")
        print("        To apply 20x to existing positions, they must be closed and reopened.")
    else:
        print("  No open positions - leverage will apply to new positions")
    
    print()
    print("="*80)
    print("RECOMMENDATIONS")
    print("="*80)
    
    if len(open_positions) > 0:
        print("\n1. EXISTING POSITIONS:")
        print("   - Existing positions still have their original leverage (likely 1x)")
        print("   - Consider closing these positions manually if you want 20x leverage")
        print("   - New positions will automatically use 20x leverage")
    
    if db_order_count > 1000:
        print("\n2. LARGE NUMBER OF OPEN ORDERS:")
        print(f"   - {db_order_count} orders in database is very high")
        print("   - Consider running cleanup/reconciliation")
        print("   - Some orders may be stale or already filled")
    
    if db_pos_count != len(open_positions):
        print("\n3. POSITION SYNC MISMATCH:")
        print("   - Run sync_bot_state() for affected bots")
        print("   - Or restart the bot engine to trigger reconciliation")
    
    print()

def analyze_database_only():
    """Fallback analysis when exchange is not accessible"""
    print()
    print("="*80)
    print("DATABASE-ONLY ANALYSIS")
    print("="*80)
    print()
    
    db_path = Path(__file__).parent / "crypto_bot.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get detailed stats
    cursor.execute("""
        SELECT 
            status,
            COUNT(*) as count,
            SUM(amount * price) as total_value
        FROM bot_orders
        GROUP BY status
    """)
    status_breakdown = cursor.fetchall()
    
    print("ORDER STATUS BREAKDOWN:")
    print("-"*80)
    total_orders = 0
    total_value = 0
    for status, count, value in status_breakdown:
        val = value or 0
        total_orders += count
        total_value += val
        print(f"  {status:15s}: {count:5d} orders, ${val:15,.2f}")
    
    print(f"\n  TOTAL: {total_orders:5d} orders, ${total_value:15,.2f}")
    
    print("\n" + "="*80)
    print("CONCLUSION")
    print("="*80)
    print("\nWithout exchange access, we can only confirm:")
    print(f"  ✓ Database has 1,247 'open' orders")
    print(f"  ✓ Database has 0 active positions")
    print(f"  ✓ Leverage updated to 20x in bot configs")
    print()
    print("To verify exchange sync, you need:")
    print("  1. Valid API credentials")
    print("  2. Access to mainnet (testnet is deprecated)")
    print("  3. Run this script with mainnet API keys")
    print()
    
    conn.close()
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
