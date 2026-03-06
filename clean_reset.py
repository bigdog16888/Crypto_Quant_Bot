"""
clean_reset.py
--------------
TESTNET ONLY — Full clean slate:
  1. Cancels ALL open exchange orders
  2. Closes ALL open exchange positions (market orders)
  3. Resets ALL bot DB state to SCANNING (total_invested=0, step=0)

Run AFTER stopping the engine (engine must be stopped first).
Usage: python clean_reset.py
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface, normalize_symbol

print("\n" + "="*70)
print("  CLEAN RESET — TESTNET FULL WIPE")
print("="*70)

# ── Safety check: warn if engine is running ───────────────────────────────
try:
    pid_file = os.path.join(os.path.dirname(__file__), 'engine.pid')
    if os.path.exists(pid_file):
        with open(pid_file) as f:
            pid = int(f.read().strip())
        import psutil
        if psutil.pid_exists(pid):
            print(f"⛔ Engine appears to be running (PID {pid}).")
            print("   Please stop the engine from the UI first, then re-run this script.")
            sys.exit(1)
except Exception:
    pass  # If we can't check, proceed cautiously

print("\n⚠️  This will:")
print("   1. Cancel ALL open exchange orders")
print("   2. Market-close ALL open exchange positions")
print("   3. Reset ALL bot DB records to SCANNING state")
confirm = input("\nType 'RESET' to confirm: ")
if confirm.strip() != 'RESET':
    print("Aborted.")
    sys.exit(0)

# ── Step 1 & 2: Clear Exchange ──────────────────────────────────────────
for m_type in ['spot', 'future']:
    print(f"\n[{'1' if m_type == 'spot' else '2'}/3] Cleaning {m_type.upper()} market...")
    try:
        ex = ExchangeInterface(market_type=m_type)
        
        # 1. Cancel Orders
        orders = ex.fetch_open_orders()
        if orders:
            print(f"  Found {len(orders)} open {m_type} orders. Cancelling...")
            for o in orders:
                try:
                    ex.cancel_order(o['id'], o['symbol'])
                    print(f"    ✅ Cancelled {o['symbol']} order {o['id']}")
                except Exception as e:
                    print(f"    ⚠️  Failed to cancel {o['symbol']}: {e}")
        else:
            print(f"  No open {m_type} orders.")

        # 2. Flatten Positions
        positions = ex.fetch_positions()
        open_pos = [p for p in (positions or []) if abs(float(p.get('contracts', 0) or p.get('size', 0) or 0)) > 0.00001]
        if open_pos:
            print(f"  Found {len(open_pos)} open {m_type} positions. Closing...")
            for p in open_pos:
                sym = p['symbol']
                size = float(p.get('contracts', 0) or p.get('size', 0))
                # Side is opposite of current position
                side = 'sell' if size > 0 else 'buy'
                try:
                    ex.create_order(sym, 'market', side, abs(size))
                    print(f"    ✅ Closed {sym}: {size} ({side})")
                    time.sleep(0.2)
                except Exception as e:
                    print(f"    ⚠️  Failed to close {sym}: {e}")
        else:
            print(f"  No open {m_type} positions.")

    except Exception as e:
        print(f"  ❌ Error cleaning {m_type} market: {e}")


# ── Step 3: Reset DB ──────────────────────────────────────────────────────
print("\n[3/3] Resetting bot DB state...")
conn = get_connection()
cursor = conn.cursor()

try:
    now = int(time.time())

    # Delete Recovered_Bot_* entries completely (they were created by a one-off
    # recovery script and should never come back — real bots have proper names).
    cursor.execute("SELECT COUNT(*) FROM bots WHERE name LIKE 'Recovered_Bot_%'")
    recovered_count = cursor.fetchone()[0]
    if recovered_count > 0:
        cursor.execute("DELETE FROM trades WHERE bot_id IN (SELECT id FROM bots WHERE name LIKE 'Recovered_Bot_%')")
        cursor.execute("DELETE FROM bot_orders WHERE bot_id IN (SELECT id FROM bots WHERE name LIKE 'Recovered_Bot_%')")
        cursor.execute("DELETE FROM bots WHERE name LIKE 'Recovered_Bot_%'")
        print(f"  ✅ Deleted {recovered_count} Recovered_Bot entries from bots table")
    else:
        print("  ℹ️  No Recovered_Bot entries found to delete")

    # Reset all trades rows to zero
    cursor.execute("""
        UPDATE trades SET
            current_step     = 0,
            total_invested   = 0,
            avg_entry_price  = 0,
            target_tp_price  = 0,
            entry_confirmed  = 0,
            basket_start_time = ?,
            last_exit_price  = 0,
            last_exit_time   = ?,
            entry_order_id   = NULL,
            tp_order_id      = NULL,
            bot_position_id  = NULL,
            close_type       = 'MANUAL_RESET',
            cycle_id         = COALESCE(cycle_id, 1) + 1
    """, (now, now))
    trades_reset = cursor.rowcount

    # Set all bots to Scanning
    cursor.execute("UPDATE bots SET status = 'Scanning' WHERE is_active = 1")
    bots_reset = cursor.rowcount

    # Mark all open bot_orders as cancelled
    cursor.execute("UPDATE bot_orders SET status = 'cancelled' WHERE status IN ('open', 'pending')")
    orders_reset = cursor.rowcount

    conn.commit()
    print(f"  ✅ Reset {trades_reset} trade records")
    print(f"  ✅ Set {bots_reset} bots to Scanning")
    print(f"  ✅ Cancelled {orders_reset} pending bot_orders in DB")

except Exception as e:
    conn.rollback()
    print(f"  ❌ DB reset failed: {e}")
finally:
    conn.close()

print("\n" + "="*70)
print("  DONE. You can now restart the engine.")
print("  The bots will start fresh in SCANNING mode.")
print("="*70 + "\n")
