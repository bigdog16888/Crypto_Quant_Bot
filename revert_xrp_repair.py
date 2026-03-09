import sqlite3
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from engine.database import DB_PATH, get_connection

def fix_total_invested(bot_id, pair):
    conn = get_connection()
    c = conn.cursor()

    # Get current cycle_id
    c.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,))
    row = c.fetchone()
    if not row:
        print(f"Bot {bot_id} not found in trades.")
        return
    cycle_id = row[0]

    # Sum the ACTUAL real filled orders (exclude dummy repair entries)
    c.execute("""
        SELECT SUM(amount * price), SUM(amount * price) / NULLIF(SUM(amount), 0), SUM(amount)
        FROM bot_orders
        WHERE bot_id=? AND cycle_id=? AND status='filled'
          AND order_type IN ('entry', 'grid')
          AND order_id NOT LIKE 'GAP_REPAIR%'
          AND order_id NOT LIKE 'MANUAL_%'
    """, (bot_id, cycle_id))
    result = c.fetchone()
    real_total = result[0] or 0.0
    real_avg_price = result[1] or 0.0
    real_qty = result[2] or 0.0

    print(f"\nBot {bot_id} ({pair}):")
    print(f"  Real total_invested (from actual fills): ${real_total:.2f}")
    print(f"  Real avg_entry_price:                    {real_avg_price:.6f}")
    print(f"  Real total qty:                          {real_qty:.4f}")

    # Get current DB values
    c.execute("SELECT total_invested, avg_entry_price FROM trades WHERE bot_id=?", (bot_id,))
    cur = c.fetchone()
    print(f"  Current DB total_invested:               ${cur[0]:.2f}")
    print(f"  Current DB avg_entry_price:              {cur[1]:.6f}")

    # Delete the dummy gap repair orders
    c.execute("DELETE FROM bot_orders WHERE bot_id=? AND order_id LIKE 'GAP_REPAIR%'", (bot_id,))
    deleted = c.rowcount
    print(f"  Deleted {deleted} GAP_REPAIR dummy entries from bot_orders.")

    # Fix the trades table
    c.execute("""
        UPDATE trades SET
            total_invested = ?,
            avg_entry_price = ?
        WHERE bot_id = ?
    """, (real_total, real_avg_price, bot_id))

    conn.commit()
    conn.close()
    print(f"  ✅ FIXED: total_invested set to ${real_total:.2f}, avg_entry_price set to {real_avg_price:.6f}")

if __name__ == "__main__":
    fix_total_invested(10017, "XRP/USDC:USDC")
