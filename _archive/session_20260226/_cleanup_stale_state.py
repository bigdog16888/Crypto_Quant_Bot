"""
Universal Clean-Slate DB Reset Script
--------------------------------------
Use after flattening the exchange (all positions closed).
Resets ALL bots to Scanning state: clears trades, open orders in DB.
Keeps bot config (pair, direction, strategy settings) intact.

Usage:
    python _cleanup_stale_state.py
"""
import sqlite3
import time

DB_PATH = 'crypto_bot.db'

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("UNIVERSAL CLEAN-SLATE DB RESET")
print("Exchange must be FLAT (all positions closed) before running.")
print("=" * 60)

# --- BEFORE STATE ---
print("\n=== BOT STATE BEFORE RESET ===")
c.execute("""
    SELECT b.id, b.name, b.status, b.pair,
           COALESCE(t.total_invested, 0), COALESCE(t.current_step, 0),
           t.entry_order_id, t.tp_order_id
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY b.id
""")
rows = c.fetchall()
for r in rows:
    bid, bname, bstatus, bpair, inv, step, eid, tid = r
    print(f"  Bot {bid} ({bname}) [{bpair}]: status={bstatus}  invested=${inv:.2f}  step={step}  entry_id={eid}  tp_id={tid}")

# --- STEP 1: Reset all trades for active bots ---
print("\n[1/4] Resetting all trade states to IDLE...")
c.execute("""
    UPDATE trades
    SET current_step       = 0,
        total_invested     = 0,
        avg_entry_price    = 0,
        target_tp_price    = 0,
        entry_confirmed    = 0,
        entry_order_id     = NULL,
        tp_order_id        = NULL,
        basket_start_time  = ?,
        close_type         = 'CLEAN_SLATE_RESET'
    WHERE bot_id IN (SELECT id FROM bots WHERE is_active = 1)
""", (int(time.time()),))
print(f"   → Reset {c.rowcount} trade record(s).")

# --- STEP 2: Mark all open bot_orders as cancelled ---
print("[2/4] Closing all open orders in bot_orders...")
c.execute("""
    UPDATE bot_orders
    SET status     = 'auto_closed',
        updated_at = ?
    WHERE status = 'open'
      AND bot_id IN (SELECT id FROM bots WHERE is_active = 1)
""", (int(time.time()),))
print(f"   → Closed {c.rowcount} open order record(s).")

# --- STEP 3: Set all active bots to 'Scanning' ---
print("[3/4] Setting all active bots to 'Scanning' status...")
c.execute("""
    UPDATE bots
    SET status = 'Scanning'
    WHERE is_active = 1
""")
print(f"   → Updated {c.rowcount} bot(s) to Scanning.")

# --- STEP 4: Clear active_positions snapshot ---
print("[4/4] Clearing active_positions snapshot...")
c.execute("DELETE FROM active_positions")
print(f"   → Cleared {c.rowcount} position snapshot(s).")

conn.commit()

# --- AFTER STATE ---
print("\n=== BOT STATE AFTER RESET ===")
c.execute("""
    SELECT b.id, b.name, b.status, COALESCE(t.total_invested,0), t.entry_order_id, t.tp_order_id
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY b.id
""")
for r in c.fetchall():
    bid, bname, bstatus, inv, eid, tid = r
    status_icon = "✅" if inv == 0 and bstatus == 'Scanning' else "⚠️"
    print(f"  {status_icon} Bot {bid} ({bname}): status={bstatus}  invested=${inv:.2f}  entry_id={eid}  tp_id={tid}")

open_orders_remaining = c.execute("SELECT COUNT(*) FROM bot_orders WHERE status='open'").fetchone()[0]
print(f"\nOpen orders remaining in DB: {open_orders_remaining} (should be 0)")
print("\n✅ Clean-slate reset complete. Start the engine to begin fresh trading.")
conn.close()
