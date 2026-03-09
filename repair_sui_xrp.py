import sqlite3
import time
from engine.database import DB_PATH, get_connection, accumulate_trade_fill

def repair_bot(bot_id, gap_usd, pair):
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get current avg_entry_price
    cursor.execute("SELECT avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=?", (bot_id,))
    row = cursor.fetchone()
    if not row:
        print(f"Bot {bot_id} not found in trades.")
        return
        
    avg_price = row[0]
    current_step = row[1]
    cycle_id = row[2]
    
    # Calculate amount
    gap_amount = gap_usd / avg_price
    
    # Insert dummy fill into bot_orders
    order_id = f"GAP_REPAIR_{int(time.time())}_{bot_id}"
    cid = f"CQB_{bot_id}_GRID_{current_step}_REPAIR"
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, updated_at, client_order_id, cycle_id, notes)
        VALUES (?, ?, 'grid', ?, ?, ?, 'filled', ?, ?, ?, ?, 'Manual Gap Repair from Console')
    """, (bot_id, current_step, order_id, avg_price, gap_amount, int(time.time()), int(time.time()), cid, cycle_id))
    
    conn.commit()
    conn.close()
    
    # Accumulate the fill to push it into 'trades'
    accumulate_trade_fill(bot_id, gap_usd, gap_amount, avg_price, current_step, 0.0, is_entry=True)
    print(f"✅ Bot {bot_id} ({pair}) repaired with +${gap_usd:.2f} Notional inserted.")

if __name__ == "__main__":
    repair_bot(10017, 397.63, "XRP/USDC:USDC")
    repair_bot(10018, 554.09, "SUI/USDC:USDC")
