import sqlite3
import time

def forensic_seal_xau():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    bot_id = 10019
    pair = 'XAUUSDT:USDT'
    
    print(f"--- Forensic Seal for Bot {bot_id} ({pair}) ---")
    
    # 1. Get current state
    cur.execute("SELECT status FROM bots WHERE id=?", (bot_id,))
    status = cur.fetchone()[0]
    cur.execute("SELECT current_step, total_invested, open_qty, avg_entry_price, cycle_id FROM trades WHERE bot_id=?", (bot_id,))
    step, invested, qty, price, cycle = cur.fetchone()
    
    print(f"Current State: Status={status}, Step={step}, Cycle={cycle}")
    print(f"Virtual Position: {qty} units (${invested:.2f}) @ ${price:.2f}")
    
    # 2. Update trades table to 0
    cur.execute("""
        UPDATE trades 
        SET total_invested = 0, open_qty = 0, avg_entry_price = 0, 
            current_step = 0,
            entry_order_id = NULL, tp_order_id = NULL, 
            last_exit_time = ?,
            cycle_phase = 'IDLE'
        WHERE bot_id = ?
    """, (int(time.time()), bot_id))
    
    # 3. Update bot status to Scanning
    cur.execute("UPDATE bots SET status = 'Scanning' WHERE id = ?", (bot_id,))
    
    # 4. Mark active orders as reset_cleared
    cur.execute("""
        UPDATE bot_orders 
        SET status = 'reset_cleared', notes = notes || ' | Forensic seal - mismatch with exchange'
        WHERE bot_id = ? AND status = 'open'
    """, (bot_id,))
    
    # 5. Insert a special history record
    cur.execute("""
        INSERT INTO trade_history (bot_id, timestamp, action, symbol, price, amount, pnl, notes)
        VALUES (?, ?, 'SYSTEM_WIPE', ?, 0.0, 0.0, 0.0, ?)
    """, (bot_id, int(time.time()), pair, f"Forensic Seal (Cycle {cycle}): Mismatch found by IntegrityEnforcer. SystemNet -0.012 vs Exchange 0.0"))
    
    conn.commit()
    print("✅ Bot 10019 has been sealed and reset to Scanning.")
    conn.close()

if __name__ == "__main__":
    forensic_seal_xau()
