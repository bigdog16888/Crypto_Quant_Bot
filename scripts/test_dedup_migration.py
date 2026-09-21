#!/usr/bin/env python3
"""
Corrected dedup migration test for active_positions table.
Cross-checks against trades.open_qty to pick the correct row.
Operates on TEST COPY ONLY - does not touch live DB.
"""
import sqlite3
import time
import sys
import os

# Add project root to path
sys.path.insert(0, 'D:/Crypto_Quant_Bot')
from engine.exchange_interface import normalize_symbol

LIVE_DB = 'D:/Crypto_Quant_Bot/crypto_bot.db'
TEST_DB = 'D:/Crypto_Quant_Bot/crypto_bot_migration_test_v4.db'

def main():
    # Step 0: Create fresh test copy
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    import shutil
    shutil.copy2(LIVE_DB, TEST_DB)
    print(f"[OK] Test copy created: {TEST_DB}")
    
    conn = sqlite3.connect(TEST_DB)
    c = conn.cursor()
    
    # Step 1: Show reference state (live DB)
    print("\n=== REFERENCE STATE (Live DB) ===")
    for bot_id in [10019, 100324]:
        c_live = sqlite3.connect(LIVE_DB).cursor()
        
        c_live.execute('SELECT name, pair FROM bots WHERE id=?', (bot_id,))
        bot_name, bot_pair = c_live.fetchone()
        
        c_live.execute('SELECT open_qty, total_invested, cycle_id, cycle_phase FROM trades WHERE bot_id=?', (bot_id,))
        oq, inv, cid, phase = c_live.fetchone()
        
        print(f"\nBot {bot_id} ({bot_name}):")
        print(f"  trades.open_qty={oq:.6f}, invested={inv:.2f}, cycle={cid}, phase={phase}")
        
        c_live.execute('''
            SELECT pair, side, size, last_updated 
            FROM active_positions 
            WHERE bot_id=? 
            ORDER BY last_updated DESC
        ''', (bot_id,))
        rows = c_live.fetchall()
        print(f"  active_positions rows ({len(rows)}):")
        for r in rows:
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[3])) if r[3] else 'NULL'
            match = '✓ MATCHES trades' if abs(r[2] - oq) < 0.001 else '✗ MISMATCH'
            print(f"    pair={r[0]}, side={r[1]}, size={r[2]:.6f}, updated={ts_str} {match}")
        c_live.close()
    
    # Step 2: Run migration on test DB
    print("\n\n=== RUNNING MIGRATION ON TEST DB ===")
    
    # Add normalized_pair column
    try:
        c.execute("ALTER TABLE active_positions ADD COLUMN normalized_pair TEXT")
        print("[OK] Added normalized_pair column")
    except:
        print("[INFO] normalized_pair column already exists")
    
    # Populate normalized_pair
    c.execute("SELECT bot_id, pair, side, size FROM active_positions")
    rows = c.fetchall()
    for row in rows:
        new_pair = normalize_symbol(row[1])
        c.execute("UPDATE active_positions SET normalized_pair=? WHERE bot_id=? AND pair=? AND side=? AND size=?", 
                  (new_pair, row[0], row[1], row[2], row[3]))
    conn.commit()
    print(f"[OK] Populated normalized_pair for {len(rows)} rows")
    
    # Find duplicate groups
    c.execute("""
        SELECT bot_id, normalized_pair, side, COUNT(*) as cnt
        FROM active_positions
        GROUP BY bot_id, normalized_pair, side
        HAVING cnt > 1
        ORDER BY bot_id
    """)
    dupes = c.fetchall()
    print(f"\n[INFO] Found {len(dupes)} duplicate groups:")
    for d in dupes:
        print(f"  bot_id={d[0]}, pair={d[1]}, side={d[2]}, count={d[3]}")
    
    # Step 3: Apply corrected dedup logic
    print("\n=== APPLYING CORRECTED DEDUP LOGIC ===")
    print("Rule: Keep row whose size matches trades.open_qty (within 0.001)")
    print("      If no match, keep most recent (latest last_updated)")
    print()
    
    for d in dupes:
        bot_id, norm_pair, side = d[0], d[1], d[2]
        
        # Get trades.open_qty for this bot
        c.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,))
        trade_row = c.fetchone()
        trade_open_qty = float(trade_row[0] or 0) if trade_row else 0
        
        # Get all candidate rows
        c.execute("""
            SELECT rowid, pair, size, last_updated
            FROM active_positions
            WHERE bot_id=? AND normalized_pair=? AND side=?
            ORDER BY last_updated DESC
        """, (bot_id, norm_pair, side))
        candidates = c.fetchall()
        
        print(f"Bot {bot_id} (trades.open_qty={trade_open_qty:.6f}):")
        for cand in candidates:
            rowid, orig_pair, size, ts = cand
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else 'NULL'
            match = '✓ MATCHES' if abs(size - trade_open_qty) < 0.001 else '✗ MISMATCH'
            action = 'KEEP' if abs(size - trade_open_qty) < 0.001 else 'DELETE'
            print(f"  rowid={rowid}, pair={orig_pair}, size={size:.6f}, updated={ts_str} {match} -> {action}")
        
        # Determine which to keep
        keep_rowid = None
        for cand in candidates:
            rowid, orig_pair, size, ts = cand
            if abs(size - trade_open_qty) < 0.001:
                keep_rowid = rowid
                break
        
        # Fallback: keep most recent if no match
        if keep_rowid is None and candidates:
            keep_rowid = candidates[0][0]
        
        print(f"  Decision: KEEP rowid={keep_rowid}")
        
        # Delete non-kept rows
        if keep_rowid:
            del_ids = [cand[0] for cand in candidates if cand[0] != keep_rowid]
            if del_ids:
                placeholders = ','.join('?' * len(del_ids))
                c.execute(f"DELETE FROM active_positions WHERE rowid IN ({placeholders})", del_ids)
                print(f"  Deleted {len(del_ids)} duplicate row(s)")
        
        conn.commit()
    
    # Step 4: Show post-dedup state
    print("\n\n=== POST-DEDUP STATE (Test DB) ===")
    c.execute("SELECT COUNT(*) FROM active_positions")
    print(f"Total rows: {c.fetchone()[0]}")
    
    print("\n--- Duplicate bots only ---")
    for d in dupes:
        bot_id, norm_pair, side = d[0], d[1], d[2]
        c.execute("""
            SELECT bot_id, pair, normalized_pair, side, size, last_updated
            FROM active_positions
            WHERE bot_id=? AND normalized_pair=? AND side=?
        """, (bot_id, norm_pair, side))
        for r in c.fetchall():
            ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[5])) if r[5] else 'NULL'
            print(f"  bot_id={r[0]}, pair={r[1]}, norm={r[2]}, size={r[4]:.6f}, updated={ts_str}")
    
    # Step 5: Verify no remaining duplicates
    print("\n=== VERIFICATION ===")
    c.execute("""
        SELECT bot_id, normalized_pair, side, COUNT(*) as cnt
        FROM active_positions
        GROUP BY bot_id, normalized_pair, side
        HAVING cnt > 1
    """)
    remaining = c.fetchall()
    print(f"Remaining duplicates: {len(remaining)}")
    
    # Create UNIQUE index
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_positions_unique ON active_positions(bot_id, normalized_pair, side)")
        print("[OK] Created UNIQUE index")
    except Exception as e:
        print(f"[WARN] Index creation failed: {e}")
        conn.rollback()
    
    conn.commit()
    conn.close()
    
    print("\n=== MIGRATION COMPLETE ===")
    print(f"Test DB: {TEST_DB}")
    print("Live DB was NOT modified.")

if __name__ == '__main__':
    main()
