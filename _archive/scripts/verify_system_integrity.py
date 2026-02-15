import sqlite3
import os
import sys
import time
from tabulate import tabulate

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

def verify_system_integrity():
    print("🕵️ STARTING SYSTEM INTEGRITY CHECK...")
    print("=======================================")
    
    # 1. DB State Analysis
    print("\n1. DATABASE STATE (The Brain)")
    conn = get_connection()
    cursor = conn.cursor()
    
    # Active Bots
    cursor.execute("SELECT count(*) FROM bots WHERE is_active=1")
    active_bots_count = cursor.fetchone()[0]
    print(f"   🤖 Active Bots (Scanning): {active_bots_count}")
    
    # Bots In Trade
    cursor.execute("SELECT count(*) FROM trades WHERE total_invested > 0")
    bots_in_trade_count = cursor.fetchone()[0]
    print(f"   📈 Bots In Trade: {bots_in_trade_count}")
    
    # Expected Orders
    cursor.execute("""
        SELECT b.id, b.name, b.pair, t.total_invested, t.current_step 
        FROM trades t JOIN bots b ON t.bot_id = b.id 
        WHERE t.total_invested > 0
    """)
    trades = cursor.fetchall()
    
    # 2. Exchange State Analysis
    print("\n2. EXCHANGE STATE (Reality)")
    try:
        exchange = ExchangeInterface(market_type='future')
        positions = exchange.fetch_positions()
        
        real_positions = [p for p in positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        print(f"   🏦 Open Positions on Exchange: {len(real_positions)}")
        
        open_orders = exchange.fetch_open_orders()
        print(f"   📝 Open Orders on Exchange: {len(open_orders)}")
        
    except Exception as e:
        print(f"   ❌ Exchange Connection Failed: {e}")
        return

    # 3. Reconciliation & Verification
    print("\n3. INTEGRITY REPORT")
    print("---------------------------------------")
    
    # Table Data
    table_data = []
    
    # Check 1: Bots vs Positions
    status_icon = "✅" if bots_in_trade_count == len(real_positions) else "❌"
    print(f"{status_icon} POSITION SYNC: DB says {bots_in_trade_count} bots trading | Exchange has {len(real_positions)} positions")
    
    # Virtual Position Awareness: Aggregate checks
    if bots_in_trade_count != len(real_positions):
        print("   ℹ️  Note: In Virtual Position mode, multiple bots may share a single physical position.")
        print("       Check per-bot details below for isolation verification.")

    # Check 2: Detail Mismatch
    print("\n   [Deep Dive: Per-Bot Verification]")
    
    # Map real positions by symbol
    pos_map = {p['symbol']: float(p.get('contracts', 0) or p.get('size', 0)) for p in real_positions}
    
    for t in trades:
        bot_id, name, pair, invested, step = t
        # Convert pair to exchange format if needed (simple check)
        # Normalize DB pair BTC/USDC -> BTC/USDC:USDC
        # This is a basic normalization for the check
        found = False
        match_size = 0.0
        
        # Fuzzy match symbol
        for sym, size in pos_map.items():
            if pair.replace('/', '') in sym.replace('/', ''):
                found = True
                match_size = size
                break
        
        # Check logic
        if found:
            # We don't have exact entry price here easily to calc expected size, 
            # but we know size should be > 0.
            # In a real deep check we'd compare invested vs notional.
            # For now, just existence is good.
            table_data.append([name, pair, "IN TRADE", f"{invested:.2f}", f"{match_size:.4f}", "✅ MATCH"])
        else:
            table_data.append([name, pair, "IN TRADE", f"{invested:.2f}", "0.0", "❌ GHOST"])

    # Check for Orphans (Positions with no Bot)
    bot_pairs = [t[2].replace('/', '') for t in trades]
    for sym, size in pos_map.items():
        clean_sym = sym.replace('/', '').split(':')[0]
        is_tracked = False
        for bp in bot_pairs:
            if bp in clean_sym:
                is_tracked = True
        
        if not is_tracked:
             table_data.append(["UNKNOWN", sym, "N/A", "N/A", f"{size:.4f}", "❌ ORPHAN"])

    if not table_data:
        print("   (No active trades to verify - System is Idle/Clean)")
    else:
        print(tabulate(table_data, headers=["Bot Name", "Pair", "DB State", "Invested ($)", "Exch Size", "Status"], tablefmt="grid"))

    # Check 3: Order Logic (Are triggers working?)
    # If active bots > 0 and trades = 0, we are "Scanning".
    # If we have trades, we expect orders.
    
    print("\n4. ORDER LOGIC CHECK")
    expected_orders = 0
    if bots_in_trade_count > 0:
        # Simplification: Each trade should have at least 1 TP or Grid
        expected_orders = bots_in_trade_count # Min 1 per bot
    
    print(f"   Expected Open Orders (Min): {expected_orders}")
    print(f"   Actual Open Orders: {len(open_orders)}")
    
    if len(open_orders) < expected_orders:
        print("   ⚠️ WARNING: Fewer orders than expected. Bots might be exposed (Naked Position).")
    elif len(open_orders) > expected_orders * 10:
        print("   ⚠️ WARNING: Too many orders? Check for spam.")
    else:
        print("   ✅ Order count seems reasonable.")

    print("\n=======================================")
    if bots_in_trade_count == len(real_positions) and (bots_in_trade_count == 0 or len(open_orders) >= bots_in_trade_count):
        print("🟢 SYSTEM INTEGRITY: PASSED")
        print("   The system is robust and synchronized.")
    else:
        print("🔴 SYSTEM INTEGRITY: FAILED / ATTENTION NEEDED")
        print("   Review the errors above.")

if __name__ == "__main__":
    verify_system_integrity()
