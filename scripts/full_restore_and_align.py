# ==============================================================================
# 🚨 EMERGENCY OPERATIONAL SAFETY SAFEGUARDS (PERMANENT RULE) 🚨
# ==============================================================================
# Any database restoration or alignment script MUST adhere to the following rules:
#
# 1. ENGINE CONFIRMATION:
#    The trading engine MUST be fully confirmed STOPPED and offline. Check that no
#    active python engine/runner.py or Streamlit UI processes are running.
#
# 2. LIVE POSITION VERIFICATION:
#    A final live fetch_positions check MUST be performed immediately before the
#    restore to catch any last-second fills that executed since the last scan.
#
# 3. SINGLE RUN RULE:
#    The restore/alignment script must be run EXACTLY ONCE per recovery session.
#    NEVER re-run a second time in the same recovery session without re-verifying
#    the live exchange state first, as this will lead to double-fills/over-hedging
#    due to uncommitted database states.
# ==============================================================================

import os
import sys
import shutil
import sqlite3
import time

sys.path.insert(0, os.path.abspath("."))
from engine.exchange_interface import ExchangeInterface

# ── 1. LOCKFILE SAFEGUARD CHECK ──────────────────────────────────────────────
LOCKFILE_PATH = "recovery_session.lock"
if os.path.exists(LOCKFILE_PATH):
    print(f"\n🛑 [ERROR] Safeguard triggered: Lockfile '{LOCKFILE_PATH}' exists!")
    print("This script has already run in this session. To run it again, you must")
    print("manually delete the lockfile after verifying there are no pending exchange orders.")
    sys.exit(1)

# ── 2. LIVE EXCHANGE POSITIONS MATCH SAFEGUARD CHECK ─────────────────────────
expected_positions = {
    'BNB/USDC': -0.0100,
    'SOL/USDC': 0.2900,
    'BTC/USDC': 0.0780,
    'ETH/USDC': 0.9100,
    'XRP/USDC': 145.4000
}

print("\n🔍 [SAFEGUARD] Fetching live exchange positions for verification...")
try:
    ex = ExchangeInterface()
    positions = ex.fetch_positions()
    
    live_positions = {}
    for p in positions:
        qty = float(p.get('contracts', p.get('net_qty', p.get('size', 0))) or 0)
        # If short, represent as negative
        if p.get('side') == 'short' or qty < 0:
            qty = -abs(qty)
        else:
            qty = abs(qty)
            
        if abs(qty) > 0.00001:
            symbol = p['symbol'].split(':')[0]  # Normalize: XRP/USDC:USDC -> XRP/USDC
            live_positions[symbol] = round(qty, 4)
            
    print(f"  Live Positions:     {live_positions}")
    print(f"  Expected Positions: {expected_positions}")
    
    mismatch = False
    for sym, exp_qty in expected_positions.items():
        live_qty = live_positions.get(sym, 0.0)
        if abs(live_qty - exp_qty) > 0.0001:
            print(f"🛑 [MISMATCH] {sym}: Expected {exp_qty}, but Live is {live_qty}")
            mismatch = True
            
    for sym, live_qty in live_positions.items():
        if sym not in expected_positions and abs(live_qty) > 0.0001:
            print(f"🛑 [UNEXPECTED POSITION] {sym}: Live is {live_qty}, but expected 0.0")
            mismatch = True
            
    if mismatch:
        print("\n🛑 [ERROR] Safeguard triggered: Live exchange positions do not match expected state!")
        print("Restore aborted. Align exchange positions or update expected_positions in this script.")
        sys.exit(1)
        
    print("✅ [SAFEGUARD] Exchange positions match expected state perfectly.")
except Exception as e:
    print(f"\n🛑 [ERROR] Failed to verify exchange positions: {e}")
    sys.exit(1)

# Write the lockfile
with open(LOCKFILE_PATH, "w", encoding="utf-8") as f:
    f.write(f"Locked at {time.strftime('%Y-%m-%d %H:%M:%S')} - Run complete.")
print(f"🔒 [SAFEGUARD] Lockfile '{LOCKFILE_PATH}' created.")

# ── 3. DATABASE RESTORATION AND ALIGNMENT ────────────────────────────────────

live_db = "crypto_bot.db"
live_wal = "crypto_bot.db-wal"
live_shm = "crypto_bot.db-shm"
backup_db = "backups/crypto_bot.db.sui_recovery_backup"

print("\n=== 1. CLEANING UP OLD DATABASE FILES ===")
for f in [live_db, live_wal, live_shm]:
    if os.path.exists(f):
        try:
            os.remove(f)
            print(f"  Removed: {f}")
        except Exception as e:
            print(f"  Error removing {f}: {e}")

print("\n=== 2. COPYING BACKUP FILE ===")
shutil.copy2(backup_db, live_db)
print(f"  Copied {backup_db} to {live_db}")

print("\n=== 3. REPAIRING SCHEMA ===")
conn = sqlite3.connect(live_db)
try:
    conn.execute("PRAGMA writable_schema = 1")
    conn.execute("DELETE FROM sqlite_master WHERE tbl_name='exchange_order_audit' OR sql LIKE '%exchange_order_audit%'")
    conn.commit()
    print("  Schema indices repaired successfully.")
except Exception as e:
    print(f"  Error repairing schema: {e}")
finally:
    conn.close()

print("\n=== 4. ALIGNING TRADES AND LEDGER WITH EXCHANGE REALITY ===")
# Current timestamp in seconds
now_ts = int(time.time())

# Format: bot_id -> (step, qty, avg_price, invested, side, phase)
# XRP long_hedge (100313) is now flat after the manual buy-back correction, so it is removed from active alignment.
alignment_data = {
    10007: (1, 0.010, 573.4700, 5.73, 'SHORT', 'ACTIVE'),
    10008: (2, 0.180, 80.3600, 14.46, 'LONG', 'ACTIVE'),
    10016: (6, 0.078, 62829.8500, 4900.73, 'LONG', 'ACTIVE'),
    10021: (3, 0.207, 1759.1700, 364.15, 'LONG', 'ACTIVE'),
    10017: (5, 145.40, 1.11264, 161.78, 'LONG', 'ACTIVE')
}

conn = sqlite3.connect(live_db)
conn.row_factory = sqlite3.Row
try:
    # 4.1. Set all trades to flat first, calculating individual oldest_fill-based wipe_wall_ts per bot
    rows = conn.execute("SELECT bot_id, cycle_id FROM trades").fetchall()
    for row in rows:
        bid = row['bot_id']
        cid = row['cycle_id'] or 1
        oldest_fill = conn.execute(
            "SELECT MIN(created_at) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0",
            (bid, cid)
        ).fetchone()
        oldest_ts = oldest_fill[0] if oldest_fill and oldest_fill[0] else now_ts
        wall_ts = min(now_ts, oldest_ts)
        
        conn.execute("""
            UPDATE trades
            SET current_step = 0,
                open_qty = 0.0,
                avg_entry_price = 0.0,
                total_invested = 0.0,
                cycle_phase = 'IDLE',
                entry_confirmed = 0,
                entry_order_id = NULL,
                tp_order_id = NULL,
                bot_position_id = NULL,
                close_type = NULL,
                basket_start_time = 0,
                cycle_start_time = ?,
                wipe_wall_ts = ?
            WHERE bot_id = ?
        """, (now_ts, wall_ts, bid))

    # 4.2. Apply active positions to trades and insert matching filled order to bot_orders
    for bot_id, (step, qty, avg_price, invested, side, phase) in alignment_data.items():
        # Get the current cycle_id from the backup database
        t_row = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
        cycle_id = t_row['cycle_id'] if t_row else 1
        
        # Insert a synthetic matching filled order in bot_orders for this cycle to satisfy recompute checks
        conn.execute("""
            INSERT INTO bot_orders (
                bot_id, step, order_type, order_id, price, amount, filled_amount, 
                status, created_at, updated_at, client_order_id, notes, cycle_id, filled_at, position_side
            ) VALUES (?, ?, 'entry', ?, ?, ?, ?, 'filled', ?, ?, ?, 'aligned-startup', ?, ?, ?)
        """, (
            bot_id, step, f"ALIGNED_{bot_id}", avg_price, qty, qty, 
            now_ts, now_ts, f"CQB_{bot_id}_ALIGNED_{now_ts}", cycle_id, now_ts, side
        ))
        
        # Update trades table
        oldest_fill = conn.execute(
            "SELECT MIN(created_at) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0",
            (bot_id, cycle_id)
        ).fetchone()
        oldest_ts = oldest_fill[0] if oldest_fill and oldest_fill[0] else now_ts
        wall_ts = min(now_ts, oldest_ts)

        conn.execute("""
            UPDATE trades
            SET current_step = ?,
                open_qty = ?,
                avg_entry_price = ?,
                total_invested = ?,
                position_side = ?,
                cycle_phase = ?,
                entry_confirmed = 1,
                basket_start_time = ?,
                cycle_start_time = ?,
                wipe_wall_ts = ?
            WHERE bot_id = ?
        """, (step, qty, avg_price, invested, side, phase, now_ts, now_ts, wall_ts, bot_id))
        
        # Ensure the bot's status in the bots table is set to 'IN TRADE'
        conn.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))

    # Ensure inactive/flat bots are marked as 'Scanning'
    conn.execute("UPDATE bots SET status = 'Scanning' WHERE id NOT IN (10007, 10008, 10016, 10021, 10017)")
    conn.commit()
    print("  Database alignment completed successfully.")
except Exception as e:
    print(f"  Error during alignment: {e}")
finally:
    conn.close()

# 4.3. Run global sync_trades_from_orders for all active bots to ensure no zeroed intermediate states
print("\n=== 4.3. RUNNING GLOBAL SYNC FOR ALL ACTIVE BOTS ===")
try:
    from engine.database import sync_trades_from_orders, get_connection
    conn_sync = get_connection()
    active_bots = conn_sync.execute("SELECT id FROM bots WHERE is_active = 1").fetchall()
    for (bid,) in active_bots:
        sync_trades_from_orders(bid)
    print("  Global sync completed.")
except Exception as e:
    print(f"  Error during global sync: {e}")

# Run VACUUM on a fresh connection
print("\n=== 4.5. OPTIMIZING DATABASE ===")
conn = sqlite3.connect(live_db)
try:
    conn.execute("VACUUM")
    print("  VACUUM complete.")
except Exception as e:
    print(f"  Error running VACUUM: {e}")
finally:
    conn.close()

# 5. Verify values
print("\n=== 5. VERIFYING ALIGNED DATABASE VALUES ===")
conn = sqlite3.connect(live_db)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT t.bot_id, b.name, t.current_step, t.open_qty, t.avg_entry_price, t.wipe_wall_ts, t.cycle_id
    FROM trades t JOIN bots b ON t.bot_id = b.id
    WHERE t.open_qty > 0 OR t.current_step > 0
    ORDER BY t.bot_id
""").fetchall()
for r in rows:
    print(f"  Bot: {r['bot_id']:<6} | Name: {r['name']:<20} | Step: {r['current_step']:<2} | Qty: {r['open_qty']:<8.4f} | Price: {r['avg_entry_price']:<10.4f} | WipeWall: {r['wipe_wall_ts']} | Cycle: {r['cycle_id']}")
conn.close()
