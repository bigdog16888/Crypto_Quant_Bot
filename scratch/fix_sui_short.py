import sqlite3

def fix_sui_short():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    # 1. Update bot_orders for SUI short entry order to 'filled'
    cur.execute("UPDATE bot_orders SET status = 'filled' WHERE id = 104516")
    print("Updated bot_orders ID 104516 to status = 'filled'.")
    
    # 2. Update trades for bot 100000 (sui short)
    cur.execute("""
        UPDATE trades 
        SET open_qty = 9.9,
            total_invested = 9.9198,
            avg_entry_price = 1.002,
            current_step = 1,
            cycle_id = 48,
            cycle_phase = 'ACTIVE',
            entry_confirmed = 1,
            position_side = 'SHORT'
        WHERE bot_id = 100000
    """)
    print("Updated trades row for bot 100000 to active SHORT of 9.9.")
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    fix_sui_short()
