import sqlite3

def restore_sui_long():
    backup_db = 'backups/crypto_bot_backup_20260527_135336.db'
    main_db = 'crypto_bot.db'
    
    conn_b = sqlite3.connect(backup_db)
    cur_b = conn_b.cursor()
    
    conn_m = sqlite3.connect(main_db)
    cur_m = conn_m.cursor()
    
    # 1. Fetch trades row from backup
    cur_b.execute("SELECT * FROM trades WHERE bot_id = 10018")
    trade_row = cur_b.fetchone()
    cur_b.execute("PRAGMA table_info(trades)")
    cols = [c[1] for c in cur_b.fetchall()]
    
    # 2. Update trades in main DB
    placeholders = ", ".join([f"{col} = ?" for col in cols])
    # Remove bot_id from updating list but use it in WHERE clause
    update_cols = [c for c in cols if c != 'bot_id']
    update_vals = [trade_row[cols.index(c)] for c in update_cols]
    update_vals.append(10018)
    
    query_trade = f"UPDATE trades SET {', '.join([f'{c} = ?' for c in update_cols])} WHERE bot_id = ?"
    cur_m.execute(query_trade, update_vals)
    print("Restored trades row for bot 10018.")
    
    # 3. Update bots status in main DB
    cur_m.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = 10018")
    print("Restored bots status to 'IN TRADE' for bot 10018.")
    
    # 4. Fetch bot_orders from backup and update in main DB
    cur_b.execute("SELECT id, status FROM bot_orders WHERE bot_id = 10018 AND cycle_id = 77")
    orders = cur_b.fetchall()
    for oid, status in orders:
        cur_m.execute("UPDATE bot_orders SET status = ? WHERE id = ?", (status, oid))
    print(f"Restored {len(orders)} bot_orders statuses for bot 10018.")
    
    # 5. Let's also restore 'short sui' bot status (bot 100000) to 'IN TRADE' if it wasREQUIRE_MANUAL_PROOF
    # Wait, is 'short sui' in trade or should it be manually verified?
    # Let's check 'short sui' status in backup DB
    cur_b.execute("SELECT status FROM bots WHERE id = 100000")
    status_sui_short = cur_b.fetchone()[0]
    print(f"SUI Short status in backup was: {status_sui_short}")
    
    cur_b.execute("SELECT * FROM trades WHERE bot_id = 100000")
    trade_row_short = cur_b.fetchone()
    if trade_row_short:
        # Re-set trades in main DB
        update_vals_short = [trade_row_short[cols.index(c)] for c in update_cols]
        update_vals_short.append(100000)
        cur_m.execute(query_trade, update_vals_short)
        print("Restored trades row for bot 100000.")
        
    cur_m.execute("UPDATE bots SET status = ? WHERE id = 100000", (status_sui_short,))
    print(f"Restored bots status for bot 100000 to {status_sui_short}.")
    
    # Let's also restore bot_orders for bot 100000 cycle_id 48
    cur_b.execute("SELECT id, status FROM bot_orders WHERE bot_id = 100000")
    orders_short = cur_b.fetchall()
    for oid, status in orders_short:
        cur_m.execute("UPDATE bot_orders SET status = ? WHERE id = ?", (status, oid))
    print(f"Restored {len(orders_short)} bot_orders statuses for bot 100000.")
    
    conn_m.commit()
    
    conn_b.close()
    conn_m.close()

if __name__ == '__main__':
    restore_sui_long()
