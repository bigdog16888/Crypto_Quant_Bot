
import sqlite3
import os
import sys
import json
import pandas as pd

sys.path.append(os.getcwd())
try:
    from config.settings import config
    from engine.exchange_interface import ExchangeInterface, normalize_symbol
except ImportError:
    print("Import failed.")
    sys.exit(1)

def debug_adoption():
    print("=== DEBUGGING ADOPTION LOGIC ===")
    
    # 1. Fetch Bots (Mock Runner logic)
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT b.id, b.name, b.pair, b.direction, b.status, b.config, 
               COALESCE(t.total_invested, 0), COALESCE(t.current_step, 0), 
               b.rsi_limit, b.is_active
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active=1
    """)
    bots = cursor.fetchall()
    conn.close()
    
    print(f"Loaded {len(bots)} active bots.")
    for b in bots:
        print(f"  Bot {b[0]} | Pair: {b[2]} | Norm: {normalize_symbol(b[2])}")

    # 2. Fetch Positions
    interface = ExchangeInterface()
    positions = interface.fetch_positions()
    print(f"\nFetched {len(positions)} positions.")
    
    # 3. Run Logic
    for pos in positions:
        pos_symbol = pos['symbol']
        pos_amt = pos['contracts']
        norm_pos = normalize_symbol(pos_symbol)
        
        print(f"\nChecking Position: {pos_symbol} (Norm: {norm_pos}) | Amt: {pos_amt}")
        
        if pos_amt == 0: 
            print("  Skipping (0 amount)")
            continue
            
        relevant = [b for b in bots if normalize_symbol(b[2]) == norm_pos]
        print(f"  Found {len(relevant)} relevant bots.")
        
        for bot in relevant:
            b_id = bot[0]
            b_name = bot[1]
            b_pair = bot[2]
            b_dir = bot[3]
            b_invested = float(bot[6] or 0)
            
            b_status = bot[4]
            print(f"    Candidate: Bot {b_id} ({b_name}) | Dir: {b_dir} | Status: {b_status} | Invested: {b_invested}")
            
            # Adoption Checks
            entry_price = float(pos['entryPrice'])
            exch_notional = abs(float(pos_amt)) * entry_price
            
            pos_side = pos.get('side', 'LONG').upper()
            if pos_amt > 0: pos_side_real = 'LONG'
            elif pos_amt < 0: pos_side_real = 'SHORT'
            else: pos_side_real = 'FLAT'
            
            if pos_side == 'BOTH': pos_side = pos_side_real
            
            print(f"      PosSide: {pos_side} | Real: {pos_side_real}")
            
            if b_dir.upper() != pos_side and b_dir.upper() != pos_side_real:
                print(f"      ❌ Mismatch Direction: Bot {b_dir} vs Pos {pos_side}")
                continue
                
            diff = abs(exch_notional - b_invested)
            print(f"      Diff: {diff:.2f} (Exch {exch_notional:.2f} - DB {b_invested:.2f})")
            
            if diff > 20.0:
                print(f"      ✅ ADOPTION TRIGGERED! (Diff > 20.0)")
            else:
                print(f"      ❌ No Adoption (Diff < 20.0)")

if __name__ == "__main__":
    debug_adoption()
